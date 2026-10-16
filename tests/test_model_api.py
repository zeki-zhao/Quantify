import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from quantify.jobs import JobStore, QueueManager
from quantify.lab import Lab
from quantify.model_api import (ModelError, ModelHTTP, ModelSettings, api_limits, compact_context,
                                credential, decode_response, request_body)
from quantify.model_calls import APIProposalProvider
from quantify.research_tools import ResearchTools
from quantify.worker import execute
from test_lab import proposal, wait_job

KEYS = {'OPENAI_API_KEY': 'test-openai-private-value', 'DEEPSEEK_API_KEY': 'test-deepseek-private-value'}
STOP = dict(action='stop', name='', expression='', rationale='合成协议流程结束')


def response(provider='openai', content=None, input_tokens=120, output_tokens=40):
    content = json.dumps(proposal() if content is None else content) if not isinstance(content, str) else content
    if provider == 'openai':
        body = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': content}]}],
                'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens}}
    else:
        body = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                'usage': {'prompt_tokens': input_tokens, 'completion_tokens': output_tokens}}
    return json.dumps(body).encode()


class Transport:
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def post(self, provider, body, timeout):
        self.calls.append((provider, copy.deepcopy(body), timeout))
        value = self.replies.pop(0)
        if isinstance(value, Exception): raise value
        return value() if callable(value) else value


class ModelProtocolTests(unittest.TestCase):
    def test_both_request_protocols_and_usage(self):
        limits=api_limits()
        for provider in ('openai', 'deepseek'):
            config=dict(provider=provider,model='fixture-model')
            request=request_body(config,{'objective':{'primary':'positive_rate'}},limits)
            if provider=='openai':
                self.assertFalse(request['store'])
                self.assertEqual(request['text']['format']['type'],'json_schema')
                self.assertEqual(request['max_output_tokens'],2048)
            else:
                self.assertEqual(request['response_format'],{'type':'json_object'})
                self.assertEqual(request['max_tokens'],2048)
                self.assertFalse(request['stream'])
            result=decode_response(provider,response(provider))
            self.assertEqual(result['proposal'],proposal())
            self.assertEqual(result['input_tokens'],120)
            self.assertEqual(result['output_tokens'],40)
            self.assertIsNone(result['error'])

    def test_rejects_missing_usage_refusal_truncation_and_bad_schema(self):
        with self.assertRaisesRegex(ModelError,'有效用量'):decode_response('openai',b'{}')
        for provider in ('openai','deepseek'):
            for content in ('not json',[proposal(),proposal()],{**proposal(),'python':'bad'}):
                parsed=decode_response(provider,response(provider,content))
                self.assertIsNone(parsed['proposal']);self.assertIsNotNone(parsed['error'])
            body=json.loads(response(provider))
            if provider=='openai':body['status']='incomplete'
            else:body['choices'][0]['finish_reason']='length'
            self.assertIsNotNone(decode_response(provider,json.dumps(body).encode())['error'])
        body=json.loads(response());body['usage']['input_tokens']=True
        with self.assertRaises(ModelError):decode_response('openai',json.dumps(body).encode())

    def test_transport_fixed_endpoints_headers_no_secrets_in_errors_or_redirects(self):
        with patch.dict(os.environ,KEYS):
            for provider,endpoint in [('openai','https://api.openai.com/v1/responses'),('deepseek','https://api.deepseek.com/chat/completions')]:
                http=ModelHTTP();http.opener=Mock()
                returned=Mock();returned.__enter__=Mock(return_value=returned);returned.__exit__=Mock(return_value=False)
                returned.read.return_value=response(provider)
                http.opener.open.return_value=returned
                self.assertEqual(http.post(provider,{'model':'fixture'},3),response(provider))
                request=http.opener.open.call_args.args[0]
                self.assertEqual(request.full_url,endpoint)
                self.assertTrue(request.get_header('Authorization').startswith('Bearer '))
                for code in (302,401,429,503):
                    http.opener.open.side_effect=HTTPError(endpoint,code,'secret-'+KEYS['OPENAI_API_KEY'],{},io.BytesIO(b'private response'))
                    with self.assertRaises(ModelError) as error:http.post(provider,{},3)
                    self.assertNotIn(KEYS['OPENAI_API_KEY'],str(error.exception))
                    self.assertEqual(error.exception.retryable,code in (429,503))
                http.opener.open.side_effect=URLError('proxy:password@localhost '+KEYS['OPENAI_API_KEY'])
                with self.assertRaises(ModelError) as error:http.post(provider,{},3)
                self.assertEqual(error.exception.category,'network')
                self.assertNotIn('password',str(error.exception))
                http.opener.open.side_effect=TimeoutError()
                with self.assertRaises(ModelError) as error:http.post(provider,{},3)
                self.assertFalse(error.exception.retryable)
                http.opener.open.side_effect=None;returned.read.return_value=KEYS['OPENAI_API_KEY'].encode()
                with self.assertRaises(ModelError):http.post('openai',{},3)
            with patch.dict(os.environ,{'OPENAI_API_KEY':'bad\nheader'}):
                with self.assertRaises(ModelError):credential('openai')

    def test_budget_and_context_size_validation(self):
        for value in ({'max_calls':0},{'max_retries':3},{'max_cost':0},{'endpoint':'http://localhost'}):
            with self.assertRaises(ValueError):api_limits(value)
        with self.assertRaisesRegex(ModelError,'120 KB'):
            request_body({'provider':'openai','model':'fixture'},{'text':'中'*50000},api_limits())


class ModelResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.environment=patch.dict(os.environ,KEYS);self.environment.start();self.addCleanup(self.environment.stop)
        self.lab=Lab(self.temp.name);self.addCleanup(self.lab.close)
        self.w=self.lab.workbench;self.store=self.lab.store
        snapshot=self.w.post('/api/workspace/demo',{})
        factor=next(f for f in self.store.list('factor') if f['kind']=='momentum')
        self.baseline=self.w.run(snapshot_id=snapshot['id'],factor_ids=[factor['id']])
        self.lab.close()  # Tests execute actual worker logic under deterministic transports.
        for provider in ('openai','deepseek'):
            self.lab.models.configure(provider=provider,model='fixture-model',input_price_per_million=.1,
                                      output_price_per_million=.4,currency='USD' if provider=='openai' else 'CNY')

    def session(self, provider='openai', **changes):
        return self.lab.create_session(baseline_id=self.baseline['id'],goal='仅合成API流程验证',provider=provider,**changes)

    def run_job(self,job,transport):
        owner='test-owner';claimed=self.store.claim(owner)
        self.assertEqual(claimed['id'],job['id'])
        with patch('quantify.model_calls.ModelHTTP',return_value=transport):execute(self.temp.name,job['id'],owner)
        return self.store.job(job['id'])

    def test_openai_and_deepseek_automatic_feedback_loop_and_config_snapshot(self):
        for provider in ('openai','deepseek'):
            job=self.session(provider,max_rounds=3)
            self.assertEqual(job['status'],'queued')
            self.lab.models.configure(provider=provider,model='edited-later',input_price_per_million=1,output_price_per_million=2,currency='USD')
            transport=Transport([response(provider),response(provider,STOP)])
            done=self.run_job(job,transport)
            self.assertEqual(done['status'],'succeeded',done)
            self.assertEqual(done['result']['reason'],'proposal_stop')
            self.assertFalse(done['result']['activated'])
            self.assertEqual(len(transport.calls),2)
            self.assertEqual(transport.calls[0][1]['model'],'fixture-model')
            content=transport.calls[1][1].get('input') or transport.calls[1][1]['messages']
            self.assertIn('not_improved',json.dumps(content))
            usage=self.store.model_calls.summary(job['id'])
            self.assertEqual(usage['calls'],2);self.assertEqual(usage['charged_tokens'],320)
            self.assertEqual(usage['uncertain_calls'],0)
            self.assertAlmostEqual(usage['estimated_cost'],(.1*240+.4*80)/1000000)
            exported=json.dumps(self.lab.get('/api/lab/jobs/'+job['id']))
            for key in KEYS.values():self.assertNotIn(key,exported)
            for forbidden in ('raw_csv','raw_import','DEMO1'):self.assertNotIn(forbidden,exported.split('"model_calls":',1)[1])
        self.assertNotIn(KEYS['OPENAI_API_KEY'].encode(),Path(self.temp.name,'research.sqlite3').read_bytes())

    def test_missing_key_and_pricing_block_before_request_and_settings_persist(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':''}):
            with self.assertRaisesRegex(ModelError,'OPENAI_API_KEY'):self.session()
        with self.assertRaises(ValueError):
            self.lab.models.configure(provider='openai',model='fixture',input_price_per_million=.1,output_price_per_million=.2,currency='EUR')
        view=ModelSettings(self.store).view('deepseek')
        self.assertEqual(view['model'],'fixture-model');self.assertTrue(view['key_present'])
        self.assertNotIn(KEYS['DEEPSEEK_API_KEY'],json.dumps(view))
        with tempfile.TemporaryDirectory() as other:
            fresh=ModelSettings(JobStore(other))
            self.assertFalse(fresh.view('openai')['pricing_configured'])
        with self.assertRaisesRegex(ValueError,'手动会话无需'):self.session('manual',api_budget={})

    def test_each_budget_stops_before_another_request(self):
        for budget in ({'max_total_tokens':512,'max_output_tokens':256},{'max_cost':.000001}):
            job=self.session(api_budget=budget)
            transport=Transport([]);done=self.run_job(job,transport)
            self.assertEqual(done['result']['reason'],'api_budget_exhausted')
            self.assertEqual(transport.calls,[])
            self.assertEqual(self.store.model_calls.summary(job['id'])['calls'],0)
        job=self.session(max_rounds=3,api_budget={'max_calls':1})
        transport=Transport([response()]);done=self.run_job(job,transport)
        self.assertEqual(done['result']['reason'],'api_budget_exhausted')
        self.assertEqual(len(transport.calls),1)
        self.assertEqual(done['progress']['rounds'],1)

    def test_retryable_http_attempts_each_reserved_and_auth_not_retried(self):
        job=self.session(api_budget={'max_retries':1})
        transport=Transport([ModelError('rate_limit','HTTP 429',retryable=True,http_status=429),response(content=STOP)])
        done=self.run_job(job,transport)
        self.assertEqual(done['status'],'succeeded')
        calls=self.store.model_calls.records(job['id'])
        self.assertEqual([c['attempt'] for c in calls],[1,2])
        self.assertTrue(calls[0]['usage_unknown'])
        self.assertGreater(self.store.model_calls.summary(job['id'])['charged_tokens'],160)
        job=self.session(api_budget={'max_retries':2})
        transport=Transport([ModelError('authentication','HTTP 401',http_status=401)])
        done=self.run_job(job,transport)
        self.assertEqual(done['status'],'failed');self.assertEqual(len(transport.calls),1)
        retried=self.lab.retry(job['id'])
        self.assertEqual(retried['status'],'queued');self.assertEqual(retried['retry_of'],job['id'])
        self.store.control(retried['id'],'cancel')

    def test_invalid_json_and_missing_usage_are_recorded_without_secret_bodies(self):
        for raw,reported in ((response(content='invalid json'),True),(b'{"no_usage":"private-body"}',False)):
            job=self.session()
            done=self.run_job(job,Transport([raw]))
            self.assertEqual(done['status'],'failed')
            record=self.store.model_calls.records(job['id'])[0]
            self.assertEqual(record['usage_unknown'],not reported)
            self.assertEqual(record['charged_tokens'],160 if reported else record['reserved_input_tokens']+record['reserved_output_tokens'])
            self.assertNotIn('private-body',json.dumps(record))

    def test_invalid_formula_rejected_then_feedback_to_next_model(self):
        job=self.session()
        transport=Transport([response(content=proposal('__import__("os")')),response(content=STOP)])
        done=self.run_job(job,transport)
        self.assertEqual(done['status'],'succeeded')
        self.assertEqual(done['progress']['outcomes'][0]['status'],'rejected')
        self.assertIn('rejected',json.dumps(transport.calls[1][1]))
        self.assertEqual(done['progress']['candidates'],0)
        with self.assertRaises(ValueError):self.store.append_proposals(job['id'],[proposal()])

    def test_provider_reported_usage_above_reservation_stops_and_is_charged(self):
        job=self.session()
        transport=Transport([response(input_tokens=200000)])
        done=self.run_job(job,transport)
        self.assertEqual(done['status'],'failed')
        self.assertIn('超过本地预留',done['error'])
        record=self.store.model_calls.records(job['id'])[0]
        self.assertEqual(record['charged_tokens'],200040)
        self.assertEqual(len(transport.calls),1)

    def test_pause_after_response_reuses_successful_proposal_without_http(self):
        job=self.session(max_rounds=1)
        def answer_then_pause():
            self.store.control(job['id'],'pause')
            return response()
        transport=Transport([answer_then_pause])
        paused=self.run_job(job,transport)
        self.assertEqual(paused['status'],'paused');self.assertEqual(paused['progress']['rounds'],0)
        self.assertEqual(self.store.model_calls.records(job['id'])[0]['status'],'succeeded')
        self.store.control(job['id'],'resume')
        done=self.run_job(job,transport)
        self.assertEqual(done['status'],'succeeded')
        self.assertEqual(done['progress']['rounds'],1)
        self.assertEqual(len(transport.calls),1)

    def test_inflight_timeout_is_not_retried_and_recovery_preserves_reservation(self):
        job=self.session(api_budget={'max_retries':2})
        done=self.run_job(job,Transport([ModelError('timeout','timeout result unknown')]))
        self.assertEqual(done['status'],'failed')
        self.assertEqual(len(self.store.model_calls.records(job['id'])),1)
        job=self.session()
        claimed=self.store.claim('crash-owner')
        tools=ResearchTools(self.temp.name,self.baseline['id'],claimed['progress'])
        context=tools.call('get_research_context')
        config=claimed['payload']['api']
        request=request_body(config,compact_context(context,[],claimed['payload']['goal']),config['limits'])
        self.store.model_calls.reserve(job['id'],'crash-owner',1,config,request)
        self.store.recover()
        calls=JobStore(self.temp.name).model_calls.records(job['id'])
        self.assertEqual(calls[0]['status'],'unknown')
        self.assertGreater(calls[0]['charged_tokens'],0)

    def test_holdout_changes_do_not_appear_in_outbound_package(self):
        progress={'factor_ids':self.baseline['request']['factor_ids'],'experiment_ids':[self.baseline['id']],'best_id':self.baseline['id']}
        context=ResearchTools(self.temp.name,self.baseline['id'],progress).call('get_research_context')
        encoded=json.dumps(compact_context(context,[],'test'))
        self.assertNotIn('raw_csv',encoded);self.assertNotIn('DEMO1',encoded)
        self.assertLess(context['baseline']['data_window'][-1],context['baseline']['protocol']['holdout_start'])

    def test_real_child_pause_inflight_call_keeps_unknown_reservation(self):
        from model_fixture_worker import fixture_spawn
        self.lab.models.configure(provider='openai',model='fixture-slow',input_price_per_million=.1,output_price_per_million=.4,currency='USD')
        with patch.object(QueueManager,'_spawn',fixture_spawn):
            manager=QueueManager(self.temp.name)
            try:
                job=self.session(max_rounds=1)
                deadline=__import__('time').monotonic()+10
                while not self.store.model_calls.records(job['id']):
                    if __import__('time').monotonic()>deadline: self.fail('call not reserved')
                    __import__('time').sleep(.02)
                self.store.control(job['id'],'pause')
                self.assertEqual(wait_job(self.store,job['id'],('paused',))['status'],'paused')
                row=self.store.model_calls.records(job['id'])[0]
                self.assertEqual(row['status'],'unknown')
                self.store.control(job['id'],'resume')
                done=wait_job(self.store,job['id'])
                self.assertEqual(done['status'],'failed')
                self.assertEqual(len(self.store.model_calls.records(job['id'])),1)
            finally:manager.close()
