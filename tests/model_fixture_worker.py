"""Test-only worker launcher: synthetic model HTTP, never installed in app runtime."""
import json
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def fixture_spawn(manager, job):
    return subprocess.Popen([sys.executable,str(Path(__file__).resolve()),str(manager.store.directory.resolve()),job['id'],manager.owner],
                            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def main():
    from quantify.model_api import ModelHTTP
    from quantify.worker import execute
    from quantify.jobs import JobStore
    directory,job_id,owner=sys.argv[1:]
    def post(self,provider,body,timeout):
        if body['model']=='fixture-slow':time.sleep(30)
        calls=JobStore(directory).model_calls.records(job_id)
        content=(dict(action='propose',name='合成API因子',expression='momentum(6,0)',rationale='合成流程验证') if len(calls)==1 else
                 dict(action='stop',name='',expression='',rationale='合成测试结束'))
        if provider=='openai':
            result={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(content)}]}],
                    'usage':{'input_tokens':100,'output_tokens':60}}
        else:
            result={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(content)}}],
                    'usage':{'prompt_tokens':100,'completion_tokens':60}}
        return json.dumps(result).encode()
    ModelHTTP.post=post
    execute(directory,job_id,owner)


if __name__=='__main__':main()
