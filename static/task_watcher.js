'use strict';
// Shared by diagnostic and portfolio pages. Discard responses for older watches or polls.
window.TaskWatcher=class TaskWatcher {
 constructor({request,onUpdate,onFinish,onError}) {
  Object.assign(this,{request,onUpdate,onFinish,onError});
  this.version=0;this.pollSequence=0;this.job=null;this.timer=null;
 }
 async start(submit) {
  const version=++this.version;clearTimeout(this.timer);this.job=null;this.onUpdate(null);
  try {
   const job=await submit();if(version!==this.version)return;
   this.job=job;await this.poll(version);
  }catch(error){if(version===this.version)throw error;}
 }
 async poll(version=this.version) {
  if(version!==this.version||!this.job)return;
  clearTimeout(this.timer);
  const sequence=++this.pollSequence,id=this.job.id;
  const current=()=>version===this.version&&sequence===this.pollSequence&&this.job?.id===id;
  try {
   const job=await this.request('/api/lab/jobs/'+id);if(!current())return;
   this.job=job;this.onUpdate(job);
   if(['succeeded','failed','cancelled','timed_out','interrupted'].includes(job.status)) {
    await this.onFinish(job,current);
   }else if(current())this.timer=setTimeout(()=>this.poll(version),500);
  }catch(error){if(current())this.onError(error);}
 }
 async cancel() {
  if(!this.job)return;
  const id=this.job.id,version=this.version;
  await this.request('/api/lab/control',{job_id:id,action:'cancel'});
  if(version===this.version&&this.job?.id===id)await this.poll(version);
 }
 async retry() {
  if(!this.job)return;
  const id=this.job.id;
  await this.start(()=>this.request('/api/lab/retry',{job_id:id}));
 }
};
