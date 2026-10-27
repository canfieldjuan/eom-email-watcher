import { InboxReload } from "../src/inboxReload.ts";
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import vm from 'node:vm';
const source = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8');
const names = ['observeMailboxOperationRevision', 'observeMailboxOperationRevisionFromError',
  'mailboxEffectScopeIsCurrent', 'mailboxEffectRequestIsCurrent', 'runMailAccountMutation',
  'selectInboxSender', 'queryFromInboxControls', 'commitInboxQueryFromControls', 'clearInboxPageForAccountChange', 'reconcileInboxAccountScope', 'inboxMutationInFlight',
  'deleteInboxItem', 'clearInboxHistory', 'inboxReloadBlocked', 'wakeInboxReload', 'runInboxRefresh', 'loadInboxPage', 'loadInbox', 'refreshLoadedInboxSpan', 'renderHealth', 'startConfiguredDesktop', 'renderConfigAdmissionState'];
const code = names.map(name => {
  const match = source.match(new RegExp('^(?:async )?function '+name+'(?:<[^\\n]+>)?\\([\\s\\S]*?^\\}', 'm'));
  assert.ok(match, name);
  return match[0];
}).join('\n');
const executable = stripTypeScriptTypes(code);
const noop = () => {};
function harness() {
  const calls = [];
  const node = () => ({ textContent: '', dataset: {}, hidden: false, disabled: false });
  const context = vm.createContext({
    inboxItems: [{message_id:'old-row'}], inboxNextCursor: 'page-two',
    activeInboxQuery: {provider:'imap',account_id:'old',sender:null,limit:25},
    activeInboxAccountSelection: 'active', inboxQueryEpoch: 0, inboxRequestGeneration: 0,
    inboxPageState:'loaded', inboxPageStateGeneration:0, inboxCapabilityUnavailableCount:0,
    expandedInboxMessages:new Set(), inboxDeletionsInFlight:new Set(), inboxClearInFlight:false,
    configurationReady:true, mailOperationInFlight:false,
    mailAccountMutationsInFlight:0, mailAccountMutationEpoch:0, mailboxOperationRevision:1,
    mailboxOperationRevisionReady:true, healthRequestGeneration:0,
    checkSupported:true, checkInFlight:false, gmailLabelSenderCount:{count:1},gmailLabelHealth:null,
    inboxLoadMore:node(),inboxStatus:node(),checkNow:node(),
    attachmentCapabilities:new Map(),attachmentCapabilityDiagnostics:new Map(),
    automationDecisionRefreshRequired:new Map(),
    confirmAction:async()=>true,
    inboxRemovalClient:{delete:async()=>null,clear:async()=>null},
    renderInboxSenderNavigation:noop,inboxSenderNavigationError:null,
    inboxAccountSelect:{value:'active'},inboxPageSizeSelect:{value:'25'},
    inboxSenderInput:{value:''},inboxPrioritySelect:{value:''},inboxCategorySelect:{value:''},
    inboxStateSelect:{value:''},inboxKeywordInput:{value:''},optionalFilterValue:v=>v||null,
    configuredStartupEpoch:0,configAdmissionGeneration:0,
    configInitializeForm:node(),ntfyDisclosurePanel:node(),settingsForm:node(),
    calendarConsentSettings:node(),ntfyDisclosureExpectedRevision:null,
    setConfiguredNavigation:noop,showView:noop,configAdmissionView:s=>s.state,
    settingsStatus:node(),loadMailAccounts:async()=>true,loadHealth:async()=>{},
    loadAutostart:async()=>{},loadSenders:async()=>false,finishOperation:noop,
    renderInbox:noop,clearMessageOwnedUiState:noop,setInboxControlsBusy:noop,
    refreshInboxRemovalStatuses:async()=>{},releaseAutomationRefreshFences:()=>false,
    inboxStatusLabel:()=>'',errorMessage:e=>e.message,
    loadAttachmentCapabilities:async()=>({capabilities:new Map(),diagnostics:new Map(),unavailable:0}),
    gmailLabelSenderCountAfterHealth:(_old,count)=>({count}),renderGmailLabelPollingState:noop,
    setHealthValue:noop,receivedLabel:x=>x,intervalLabel:String,watcherPrerequisitesReady:()=>true,
    mailAccounts:[{provider:'imap',account_id:'old',active:true}],
    invoke:async(operation,args)=>{
      assert.equal(operation,'inbox_query'); calls.push({...args.query});
      return {items:Array.from({length:25},(_,i)=>({message_id:args.query.cursor?'later-'+i:'first-'+i})),
        next_cursor:args.query.cursor?null:'page-two'};
    },
  });
  for(const name of ['mailHealth','mailDetail','modelHealth','modelDetail','databaseHealth',
    'databaseDetail','notificationHealth','notificationDetail','lastCheck','watchlistCount',
    'pollingCadence','nextCheck']) context[name]=node();
  context.inboxAccountForSelection=(selection='active')=>{
    const active=context.mailAccounts.find(a=>selection==='active'?a.active:a.account_id===selection);
    return {provider:active?.provider??null,account_id:active?.account_id??null};
  };
  context.renderMailAccounts=data=>{
    context.mailAccounts=data.accounts;
    return context.reconcileInboxAccountScope();
  };
  vm.runInContext(executable,context);
  context.inboxReload=new InboxReload({
    epoch:()=>context.inboxQueryEpoch, blocked:context.inboxReloadBlocked,
    currentScope:context.mailboxEffectScopeIsCurrent,run:context.runInboxRefresh,
  });
  return {c:context,calls};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const health={mail:{accounts:[{provider:'imap',account_id:'new',active:true,connected:true}],
 providers:[{provider:'imap',connection_available:true}]}, watchlist_count:1,
 local_model:{ok:true}, database:{ok:true,initialized:true},notifications:{enabled:false},
 polling:{enabled:false},gmail:{label_watch_configured:false},production_check_supported:true};

function deferred() {
  let resolve, reject;
  const promise = new Promise((a,b)=>{resolve=a;reject=b;});
  return {promise,resolve,reject};
}

test('R1: failed held account operation wakes its already-cleared scope', async()=>{
  const {c,calls}=harness();
  const hold=deferred();
  const operation=c.runMailAccountMutation(()=>hold.promise);
  c.renderHealth(health,0);
  assert.equal(calls.length,0);
  hold.reject({message:'fixture activation failed',mailbox_operation_revision:2});
  await assert.rejects(operation);
  c.renderHealth(health,0);
  await tick();
  assert.equal(calls.length,1,'settlement must retain the cleared scope load');
  assert.equal(c.inboxItems.length,25);
  assert.equal(c.inboxPageState,'loaded');
});

test('R2: actual removal completion preserves the deferred loaded span', async()=>{
  const {c,calls}=harness();
  c.inboxItems=Array.from({length:50},(_,i)=>({message_id:'retained-'+i}));
  const hold=deferred();
  c.inboxRemovalClient.delete=()=>hold.promise;
  const operation=c.deleteInboxItem({message_id:'removed',subject:'fixture'});
  await tick();
  await c.refreshLoadedInboxSpan();
  assert.equal(calls.length,0);
  hold.resolve(null);
  await operation;
  await tick();
  assert.equal(c.inboxItems.length,50,'page-one invalidation must not downgrade span');
  assert.equal(calls.length,2);
});

test('R1 control: unblocked scope and actual clear completion load the first page',async()=>{
  for(const blocked of [false,true]) {
    const {c,calls}=harness();
    const hold=deferred();
    c.inboxRemovalClient.clear=()=>hold.promise;
    const operation=blocked?c.clearInboxHistory():Promise.resolve();
    await tick();
    c.renderHealth(health,0);
    if(blocked) {assert.equal(calls.length,0);hold.resolve(null);}
    await operation;
    await tick();
    assert.equal(calls.length,1);
    assert.equal(c.inboxItems.length,25);
    assert.equal(c.inboxPageState,'loaded');
  }
});

// R1: production transition and operation owners, not hand-written finally replay.
test('R1: scope transitions survive both blocker orders and all removal settlements',async()=>{
  for(const transition of ['sender','apply','reset','health']) {
    for(const blockers of ['removal','mail','both']) {
      for(const removalFirst of [true,false]) {
        for(const outcome of ['unaccepted','error','completed','deferred']) {
          const {c,calls}=harness();
          const mail=deferred(), removal=deferred();
          c.inboxRemovalClient.delete=()=>removal.promise;
          c.inboxRemovalStatus=()=>({kind:'pending',text:'pending'});
          const deletion=blockers!=='mail'?c.deleteInboxItem({message_id:'old-row',subject:'fixture'}):null;
          await tick();
          const mutation=blockers!=='removal'?c.runMailAccountMutation(()=>mail.promise):null;
          if(mutation) mutation.catch(()=>{});
          if(transition==='sender') c.selectInboxSender('sender@example.test');
          else if(transition==='health') c.renderHealth(health,0);
          else {
            c.inboxAccountSelect.value=transition==='apply'?'new':'active';
            c.mailAccounts=health.mail.accounts;
            c.commitInboxQueryFromControls();
          }
          assert.equal(calls.length,0,`${transition}/${blockers}: no read while blocked`);
          assert.equal(c.inboxPageState,'pending');
          const settleRemoval=async()=>{
            if(!deletion)return;
            if(outcome==='error') removal.reject(new Error('fixture removal failed'));
            else removal.resolve(outcome==='unaccepted'?null:{outcome});
            await deletion;
          };
          const settleMail=async()=>{
            if(!mutation)return;
            mail.reject({message:'fixture account failed',mailbox_operation_revision:2});
            await assert.rejects(mutation);
          };
          await (removalFirst?settleRemoval():settleMail());
          if(blockers==='both') assert.equal(calls.length,0,'final blocker owns the wakeup');
          await (removalFirst?settleMail():settleRemoval());
          await tick();
          assert.equal(calls.length,1,`${transition}/${blockers}/${outcome}`);
          assert.equal(c.inboxPageState,'loaded');
          if(transition==='sender') assert.equal(calls[0].sender,'sender@example.test');
          else assert.equal(calls[0].account_id,'new');
        }
      }
    }
  }
});

test('R1: overlapping mail operations wake only after the final revision is observed',async()=>{
  const {c,calls}=harness();
  const first=deferred(),last=deferred();
  const a=c.runMailAccountMutation(()=>first.promise),b=c.runMailAccountMutation(()=>last.promise);
  c.renderHealth(health,0);
  first.resolve({mailbox_operation_revision:2});await a;
  assert.equal(calls.length,0);
  last.resolve({mailbox_operation_revision:3});await b;await tick();
  assert.equal(c.mailboxOperationRevision,3);
  assert.equal(calls.length,1);
});

test('R1: cancelled confirmation leaves current rows and requests untouched',async()=>{
  const {c,calls}=harness();c.confirmAction=async()=>false;
  await c.deleteInboxItem({message_id:'old-row',subject:'fixture'});
  await c.clearInboxHistory();
  assert.equal(calls.length,0);assert.equal(c.inboxItems[0].message_id,'old-row');
});

test('R2: span targets, exhaustion and duplicate page rows use current cursors',async()=>{
  for(const target of [0,1,25,26,50,75]) {
    const {c,calls}=harness();
    c.inboxItems=Array.from({length:target},(_,i)=>({message_id:'retained-'+i}));
    await c.refreshLoadedInboxSpan();
    assert.equal(c.inboxItems.length,target>25?50:25);
    assert.equal(calls.length,target>25?2:1);
    assert.equal(calls[0].cursor,null);
    if(target>25) assert.equal(calls[1].cursor,'page-two');
  }
  const {c}=harness();c.inboxItems=Array(50).fill({message_id:'retained'});
  c.invoke=async(_op,{query})=>({items:query.cursor?[{message_id:'same'},{message_id:'last'}]:[{message_id:'same'}],next_cursor:query.cursor?null:'next'});
  await c.refreshLoadedInboxSpan();
  assert.deepEqual(Array.from(c.inboxItems,x=>x.message_id),['same','last']);
});

test('R2: pending largest span survives first-page invalidations and repeated wakeups',async()=>{
  const {c,calls}=harness();
  const hold=deferred();c.inboxRemovalClient.clear=()=>hold.promise;
  const removal=c.clearInboxHistory();await tick();
  c.inboxItems=Array(50).fill({message_id:'retained'});await c.refreshLoadedInboxSpan();
  c.inboxItems=Array(25).fill({message_id:'retained'});await c.refreshLoadedInboxSpan();
  assert.equal(await c.loadInbox(),false);
  for(let i=0;i<5;i++) c.wakeInboxReload();
  hold.resolve(null);await removal;await tick();
  assert.equal(calls.length,2);assert.equal(c.inboxItems.length,50);
});

test('R2/R3: span interrupted between pages restarts without duplicates or old continuation',async()=>{
  const {c,calls}=harness();c.inboxItems=Array(50).fill({message_id:'retained'});
  const discovery=deferred();let discoveries=0;
  c.loadAttachmentCapabilities=async()=>{
    if(++discoveries===1)await discovery.promise;
    return {capabilities:new Map(),diagnostics:new Map(),unavailable:0};
  };
  const refresh=c.refreshLoadedInboxSpan();await tick();assert.equal(calls.length,1);
  const removalHold=deferred();c.inboxRemovalClient.delete=()=>removalHold.promise;
  const removal=c.deleteInboxItem({message_id:'removed',subject:'fixture'});await tick();
  assert.equal(calls.length,1);
  removalHold.resolve(null);await removal;await tick();
  assert.equal(c.inboxItems.length,50);assert.equal(calls.length,3);
  discovery.resolve();await refresh;await tick();
  assert.equal(calls.length,3,'superseded active span cannot append again');
  assert.equal(new Set(c.inboxItems.map(x=>x.message_id)).size,50);
});

test('R2/R3: new scope supersedes pending span and never reuses its append cursor',async()=>{
  const {c,calls}=harness();c.inboxItems=Array(50).fill({message_id:'retained'});
  const hold=deferred();const operation=c.runMailAccountMutation(()=>hold.promise);
  await c.refreshLoadedInboxSpan();
  assert.equal(await c.loadInbox(true),false);
  c.selectInboxSender('new@example.test');
  hold.resolve({mailbox_operation_revision:2});await operation;await tick();
  assert.equal(calls.length,1);assert.equal(c.inboxItems.length,25);
  assert.equal(calls[0].cursor,null);assert.equal(calls[0].sender,'new@example.test');
  c.inboxNextCursor=null;assert.equal(await c.loadInbox(true),false);assert.equal(calls.length,1);
});

test('R3/R4: old success or failure cannot delay new scope, overwrite it or acknowledge a stale caller',async()=>{
  for(const oldFirst of [true,false])for(const fails of [true,false]) {
    const {c}=harness();const old=deferred(),fresh=deferred();let calls=0,acks=0;
    c.releaseAutomationRefreshFences=()=>{acks++;return false;};
    c.invoke=()=>++calls===1?old.promise:fresh.promise;
    const oldLoad=c.loadInbox();
    c.selectInboxSender('new@example.test');
    assert.equal(calls,2,'new scope does not wait for old transport');
    const settleOld=()=>fails?old.reject(new Error('old error')):old.resolve({items:[{message_id:'stale'}],next_cursor:'stale-cursor'});
    if(oldFirst){settleOld();assert.equal(await oldLoad,false);assert.equal(c.inboxPageState,'loading');}
    fresh.resolve({items:[{message_id:'fresh'}],next_cursor:null});await tick();
    if(!oldFirst){settleOld();assert.equal(await oldLoad,false);}
    assert.equal(c.inboxPageState,'loaded');assert.equal(c.inboxItems[0].message_id,'fresh');
    assert.equal(c.inboxNextCursor,null);assert.equal(acks,1);
    assert.notEqual(c.inboxStatus.dataset.kind,'error');
  }
});

test('R3: stale scoped event is dropped; independent scope clear still loads',async()=>{
  for(const independent of [false,true]) {
    const {c,calls}=harness();c.inboxClearInFlight=true;
    assert.equal(await c.loadInbox(false,{mutation_epoch:0,mailbox_operation_revision:1}),false);
    if(independent)c.selectInboxSender('new@example.test');
    c.mailboxOperationRevision=2;c.inboxClearInFlight=false;c.wakeInboxReload();await tick();
    assert.equal(calls.length,independent?1:0);
  }
});

test('R3: configuration teardown invalidates active and pending reads; admission wakes only new work',async()=>{
  const {c}=harness();const old=deferred();let calls=0;
  c.invoke=async()=>++calls===1?old.promise:{items:[{message_id:'admitted'}],next_cursor:null};
  const loading=c.loadInbox();
  c.renderConfigAdmissionState({state:'invalid'});
  old.resolve({items:[{message_id:'stale'}],next_cursor:'stale'});
  assert.equal(await loading,false);assert.equal(c.inboxPageState,'pending');
  assert.equal(calls,1);assert.equal(c.inboxItems[0].message_id,'old-row');
  c.renderConfigAdmissionState({state:'admitted'});await tick();
  assert.equal(calls,2);assert.equal(c.inboxItems[0].message_id,'admitted');
});

test('R4: backend failure retains rows and visible error without retry; explicit retry commits',async()=>{
  const {c}=harness();let calls=0;
  c.invoke=async()=>{if(++calls===1)throw new Error('fixture query failure');return {items:[{message_id:'retry'}],next_cursor:null};};
  assert.equal(await c.loadInbox(),false);await tick();
  c.wakeInboxReload();await tick();
  assert.equal(calls,1);assert.equal(c.inboxPageState,'failed');
  assert.equal(c.inboxItems[0].message_id,'old-row');assert.equal(c.inboxStatus.textContent,'fixture query failure');
  assert.equal(await c.loadInbox(),true);assert.equal(calls,2);assert.equal(c.inboxPageState,'loaded');
});

test('R3/R4: pending work survives active completion but cannot report success to its deferred caller',async()=>{
  const {c}=harness();const first=deferred(),second=deferred();let calls=0;
  c.invoke=()=>++calls===1?first.promise:second.promise;
  const active=c.loadInbox();
  assert.equal(await c.loadInbox(),false);
  for(let i=0;i<5;i++)c.wakeInboxReload();
  assert.equal(calls,1);
  first.reject(new Error('first fails'));assert.equal(await active,false);
  assert.equal(calls,2,'independently pending request survives failure');
  second.resolve({items:[{message_id:'second'}],next_cursor:null});await tick();
  assert.equal(calls,2);assert.equal(c.inboxItems[0].message_id,'second');
});

test('R3 owner: stale scoped intent is discarded before dispatch, never rebound',async()=>{
  let blocked=true,revision=1;
  const runs=[];
  const owner=new InboxReload<number>({epoch:()=>0,blocked:()=>blocked,
    currentScope:scope=>scope===null||scope===revision,
    run:async(intent,scope)=>{runs.push({intent,scope});return true;},
  });
  assert.equal(await owner.request({kind:'span',target:50},1),false);
  revision=2;blocked=false;owner.wake();await tick();
  assert.equal(runs.length,0,'expired event must not reach the page dispatcher');
  assert.equal(await owner.request({kind:'first'}),true);
  assert.deepEqual(runs,[{intent:{kind:'first'},scope:null}],'stale span target cannot attach to independent work');
});

test('R2 owner: largest target wins in either request order without duplicate wakeups',async()=>{
  for(const targets of [[25,50],[50,25]]) {
    let blocked=true;const runs=[];
    const owner=new InboxReload({epoch:()=>0,blocked:()=>blocked,currentScope:()=>true,
      run:async(intent)=>{runs.push(intent);return true;},
    });
    for(const target of targets)assert.equal(await owner.request({kind:'span',target}),false);
    assert.equal(await owner.request({kind:'first'}),false);
    blocked=false;owner.wake();owner.wake();await tick();
    assert.deepEqual(runs,[{kind:'span',target:50}]);
  }
});

test('R1/R2: filter-only Apply replaces the blocked span and records pending',async()=>{
  const {c,calls}=harness();c.inboxItems=Array(50).fill({message_id:'retained'});
  const hold=deferred();const operation=c.runMailAccountMutation(()=>hold.promise);
  await c.refreshLoadedInboxSpan();
  c.inboxKeywordInput.value='new filter';c.commitInboxQueryFromControls();
  assert.equal(c.inboxPageState,'pending','new query must own pending state while blocked');
  assert.equal(calls.length,0);
  hold.resolve({mailbox_operation_revision:2});await operation;await tick();
  assert.equal(calls.length,1);assert.equal(calls[0].keyword,'new filter');
  assert.equal(calls[0].cursor,null);assert.equal(c.inboxItems.length,25);
});
