#!/usr/bin/env python3
import os, re, html, asyncio, logging, subprocess, shutil, zipfile, mimetypes, json, time, hashlib, contextvars
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN','').strip()
GH_TOKEN = os.getenv('GITHUB_TOKEN','').strip() or os.getenv('GH_TOKEN','').strip()
GH_OWNER = os.getenv('GITHUB_OWNER','').strip()
GH_TOKEN_2 = os.getenv('GITHUB_TOKEN_2','').strip()
GH_OWNER_2 = os.getenv('GITHUB_OWNER_2','').strip()
GH_ALIAS_1 = os.getenv('GITHUB_ALIAS_1','Akun 1').strip() or 'Akun 1'
GH_ALIAS_2 = os.getenv('GITHUB_ALIAS_2','Akun 2').strip() or 'Akun 2'
GITHUB_ACCOUNTS = {
    '1': {'alias': GH_ALIAS_1, 'owner': GH_OWNER, 'token': GH_TOKEN},
    '2': {'alias': GH_ALIAS_2, 'owner': GH_OWNER_2, 'token': GH_TOKEN_2},
}
_ACTIVE_GH_ACCOUNT = contextvars.ContextVar('github_account', default='1')
ADMINS = {int(x) for x in re.findall(r'\d+', os.getenv('TELEGRAM_ADMIN_IDS',''))}
LOCAL_BASE = Path(os.getenv('LOCAL_REPO_BASE', str(BASE/'repos'))).resolve()
LOCAL_BASE.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=getattr(logging, os.getenv('LOG_LEVEL','INFO').upper(), logging.INFO), format='%(asctime)s | %(levelname)s | %(message)s')
# Hindari library HTTP mencetak URL Telegram lengkap (yang mengandung bot token) ke log.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('telegram.request').setLevel(logging.WARNING)
log = logging.getLogger('github-control')
STATE_DIR = BASE/'data'; STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR/'state.json'; AUDIT_FILE = STATE_DIR/'audit.log'; CACHE_TTL = 20
_CACHE = {}

def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8')) if STATE_FILE.exists() else {}
    except Exception:
        return {}

def _save_state(state):
    tmp=STATE_FILE.with_suffix('.tmp'); tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(STATE_FILE)

def state_for(uid):
    s=_load_state(); return s.setdefault(str(uid),{})

def state_patch(uid,**kw):
    s=_load_state(); u=s.setdefault(str(uid),{}); u.update(kw); _save_state(s); return u

def audit(uid,repo,action,result='ok',detail=''):
    rec={'ts':datetime.now(timezone.utc).isoformat(),'uid':uid,'repo':repo,'action':action,'result':result,'detail':str(detail)[:500]}
    with AUDIT_FILE.open('a',encoding='utf-8') as f: f.write(json.dumps(rec,ensure_ascii=False)+'\n')

def cache_get(key):
    x=_CACHE.get(key)
    return x[1] if x and time.time()-x[0] < CACHE_TTL else None

def cache_set(key,val): _CACHE[key]=(time.time(),val); return val

def cache_clear(prefix=''):
    for k in list(_CACHE):
        if not prefix or str(k).startswith(prefix): _CACHE.pop(k,None)

class GHError(Exception): pass
class GH:
    def __init__(self, token):
        self.s=requests.Session(); self.s.headers.update({'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'Telegram-GitHub-Control'})
    def req(self, method, path, **kw):
        r=self.s.request(method,'https://api.github.com'+path,timeout=25,**kw)
        if r.status_code >= 400:
            try: msg=r.json().get('message',r.text[:300])
            except Exception: msg=r.text[:300]
            raise GHError(f'{r.status_code}: {msg}')
        if r.status_code==204: return None
        return r.json()
    def me(self): return self.req('GET','/user')
    def repos(self):
        out=[]; page=1
        while page<=5:
            batch=self.req('GET',f'/user/repos?per_page=100&page={page}&sort=updated&affiliation=owner,collaborator,organization_member')
            out.extend(batch)
            if len(batch)<100: break
            page += 1
        return out
    def repo(self,full): return self.req('GET',f'/repos/{full}')
    def patch_repo(self,full,**data): return self.req('PATCH',f'/repos/{full}',json=data)
    def topics(self,full): return self.req('GET',f'/repos/{full}/topics').get('names',[])
    def set_topics(self,full,names): return self.req('PUT',f'/repos/{full}/topics',json={'names':names})
    def releases(self,full): return self.req('GET',f'/repos/{full}/releases?per_page=10')
    def create_release(self,full,tag,title,body): return self.req('POST',f'/repos/{full}/releases',json={'tag_name':tag,'name':title or tag,'body':body,'generate_release_notes':not bool(body)})
    def upload_release_asset(self,full,tag,path):
        rels=self.releases(full); rel=next((x for x in rels if x.get('tag_name')==tag),None)
        if not rel: raise GHError(f'Release dengan tag {tag} tidak ditemukan')
        upload_url=rel['upload_url'].split('{',1)[0]
        ctype=mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        with open(path,'rb') as f:
            r=self.s.post(upload_url,params={'name':Path(path).name},headers={'Content-Type':ctype},data=f,timeout=180)
        if r.status_code>=400:
            try: msg=r.json().get('message',r.text[:300])
            except Exception: msg=r.text[:300]
            raise GHError(f'{r.status_code}: {msg}')
        return r.json()
    def issues(self,full): return [x for x in self.req('GET',f'/repos/{full}/issues?state=open&per_page=15') if 'pull_request' not in x]
    def create_issue(self,full,title,body): return self.req('POST',f'/repos/{full}/issues',json={'title':title,'body':body})
    def patch_issue(self,full,num,**data): return self.req('PATCH',f'/repos/{full}/issues/{num}',json=data)
    def pulls(self,full): return self.req('GET',f'/repos/{full}/pulls?state=open&per_page=15')
    def patch_pull(self,full,num,**data): return self.req('PATCH',f'/repos/{full}/pulls/{num}',json=data)
    def merge_pull(self,full,num,method='squash'): return self.req('PUT',f'/repos/{full}/pulls/{num}/merge',json={'merge_method':method})
    def create_pull(self,full,title,head,base,body=''): return self.req('POST',f'/repos/{full}/pulls',json={'title':title,'head':head,'base':base,'body':body})
    def tags(self,full): return self.req('GET',f'/repos/{full}/tags?per_page=30')
    def search_code(self,full,q): return self.req('GET',f'/search/code?q={quote(q)}+repo:{quote(full)}&per_page=20').get('items',[])
    def workflows(self,full): return self.req('GET',f'/repos/{full}/actions/workflows?per_page=30').get('workflows',[])
    def runs(self,full): return self.req('GET',f'/repos/{full}/actions/runs?per_page=12').get('workflow_runs',[])
    def dispatch(self,full,wid,ref): return self.req('POST',f'/repos/{full}/actions/workflows/{wid}/dispatches',json={'ref':ref})
    def branches(self,full): return self.req('GET',f'/repos/{full}/branches?per_page=30')
    def variables(self,full): return self.req('GET',f'/repos/{full}/actions/variables?per_page=100').get('variables',[])
    def set_variable(self,full,name,value):
        names={v['name'] for v in self.variables(full)}
        if name in names: return self.req('PATCH',f'/repos/{full}/actions/variables/{quote(name)}',json={'name':name,'value':value})
        return self.req('POST',f'/repos/{full}/actions/variables',json={'name':name,'value':value})
    def del_variable(self,full,name): return self.req('DELETE',f'/repos/{full}/actions/variables/{quote(name)}')
    def secrets(self,full): return self.req('GET',f'/repos/{full}/actions/secrets?per_page=100').get('secrets',[])
    def set_secret(self,full,name,value):
        import base64
        from nacl import encoding, public
        key=self.req('GET',f'/repos/{full}/actions/secrets/public-key')
        pk=public.PublicKey(key['key'].encode(),encoding.Base64Encoder())
        enc=public.SealedBox(pk).encrypt(value.encode())
        return self.req('PUT',f'/repos/{full}/actions/secrets/{quote(name)}',json={'encrypted_value':base64.b64encode(enc).decode(),'key_id':key['key_id']})
    def del_secret(self,full,name): return self.req('DELETE',f'/repos/{full}/actions/secrets/{quote(name)}')
    # Repository management
    def create_repo(self,name,description='',private=True,auto_init=True,owner=''):
        me=self.me().get('login',''); target=(owner or '').strip()
        path=f'/orgs/{quote(target)}/repos' if target and target.lower()!=str(me).lower() else '/user/repos'
        return self.req('POST',path,json={'name':name,'description':description,'private':private,'auto_init':auto_init})
    def delete_repo(self,full): return self.req('DELETE',f'/repos/{full}')
    def transfer_repo(self,full,new_owner,new_name=None):
        data={'new_owner':new_owner}
        if new_name: data['new_name']=new_name
        return self.req('POST',f'/repos/{full}/transfer',json=data)
    def collaborators(self,full): return self.req('GET',f'/repos/{full}/collaborators?per_page=100')
    def set_collaborator(self,full,user,permission='push'): return self.req('PUT',f'/repos/{full}/collaborators/{quote(user)}',json={'permission':permission})
    def del_collaborator(self,full,user): return self.req('DELETE',f'/repos/{full}/collaborators/{quote(user)}')
    def rulesets(self,full): return self.req('GET',f'/repos/{full}/rulesets?per_page=100')
    def rule(self,full,rid): return self.req('GET',f'/repos/{full}/rulesets/{rid}')
    def delete_ruleset(self,full,rid): return self.req('DELETE',f'/repos/{full}/rulesets/{rid}')
    def branch_protection(self,full,branch): return self.req('GET',f'/repos/{full}/branches/{quote(branch)}/protection')
    def protect_branch(self,full,branch,required_reviews=1):
        data={'required_status_checks':None,'enforce_admins':False,'required_pull_request_reviews':{'dismiss_stale_reviews':True,'require_code_owner_reviews':False,'required_approving_review_count':max(1,min(6,int(required_reviews)))},'restrictions':None,'required_linear_history':False,'allow_force_pushes':False,'allow_deletions':False,'block_creations':False,'required_conversation_resolution':True,'lock_branch':False,'allow_fork_syncing':True}
        return self.req('PUT',f'/repos/{full}/branches/{quote(branch)}/protection',json=data)
    def unprotect_branch(self,full,branch): return self.req('DELETE',f'/repos/{full}/branches/{quote(branch)}/protection')
    # Actions controls
    def run_detail(self,full,rid): return self.req('GET',f'/repos/{full}/actions/runs/{rid}')
    def cancel_run(self,full,rid): return self.req('POST',f'/repos/{full}/actions/runs/{rid}/cancel')
    def rerun(self,full,rid): return self.req('POST',f'/repos/{full}/actions/runs/{rid}/rerun')
    def rerun_failed(self,full,rid): return self.req('POST',f'/repos/{full}/actions/runs/{rid}/rerun-failed-jobs')
    def delete_run(self,full,rid): return self.req('DELETE',f'/repos/{full}/actions/runs/{rid}')
    def jobs(self,full,rid): return self.req('GET',f'/repos/{full}/actions/runs/{rid}/jobs?per_page=100').get('jobs',[])
    def download_run_logs(self,full,rid,path):
        r=self.s.get(f'https://api.github.com/repos/{full}/actions/runs/{rid}/logs',timeout=120,allow_redirects=True)
        if r.status_code>=400: raise GHError(f'{r.status_code}: gagal download workflow logs')
        Path(path).write_bytes(r.content); return path
    # Releases / assets
    def release(self,full,rid): return self.req('GET',f'/repos/{full}/releases/{rid}')
    def update_release(self,full,rid,**data): return self.req('PATCH',f'/repos/{full}/releases/{rid}',json=data)
    def delete_release(self,full,rid): return self.req('DELETE',f'/repos/{full}/releases/{rid}')
    def delete_asset(self,full,aid): return self.req('DELETE',f'/repos/{full}/releases/assets/{aid}')
    # Issue / PR details, labels, milestones
    def issue(self,full,num): return self.req('GET',f'/repos/{full}/issues/{num}')
    def issue_comments(self,full,num): return self.req('GET',f'/repos/{full}/issues/{num}/comments?per_page=30')
    def pull(self,full,num): return self.req('GET',f'/repos/{full}/pulls/{num}')
    def pull_files(self,full,num): return self.req('GET',f'/repos/{full}/pulls/{num}/files?per_page=100')
    def pull_commits(self,full,num): return self.req('GET',f'/repos/{full}/pulls/{num}/commits?per_page=100')
    def reviews(self,full,num): return self.req('GET',f'/repos/{full}/pulls/{num}/reviews?per_page=100')
    def check_runs(self,full,ref): return self.req('GET',f'/repos/{full}/commits/{quote(ref)}/check-runs?per_page=100').get('check_runs',[])
    def labels(self,full): return self.req('GET',f'/repos/{full}/labels?per_page=100')
    def create_label(self,full,name,color,description=''): return self.req('POST',f'/repos/{full}/labels',json={'name':name,'color':color.lstrip('#'),'description':description})
    def update_label(self,full,name,new_name=None,color=None,description=None):
        data={}
        if new_name is not None: data['new_name']=new_name
        if color is not None: data['color']=color.lstrip('#')
        if description is not None: data['description']=description
        return self.req('PATCH',f'/repos/{full}/labels/{quote(name)}',json=data)
    def delete_label(self,full,name): return self.req('DELETE',f'/repos/{full}/labels/{quote(name)}')
    def set_issue_labels(self,full,num,names): return self.req('POST',f'/repos/{full}/issues/{num}/labels',json={'labels':names})
    def milestones(self,full): return self.req('GET',f'/repos/{full}/milestones?state=all&per_page=100')
    def create_milestone(self,full,title,description=''): return self.req('POST',f'/repos/{full}/milestones',json={'title':title,'description':description})
    def update_milestone(self,full,num,**data): return self.req('PATCH',f'/repos/{full}/milestones/{num}',json=data)
    def close_milestone(self,full,num): return self.update_milestone(full,num,state='closed')
    # Monitoring / security
    def rate_limit(self): return self.req('GET','/rate_limit')
    def activity(self,full): return self.req('GET',f'/repos/{full}/activity?per_page=20')
    def commits(self,full,sha=None): return self.req('GET',f'/repos/{full}/commits?per_page=10'+(f'&sha={quote(sha)}' if sha else ''))
    def dependabot(self,full): return self.req('GET',f'/repos/{full}/dependabot/alerts?state=open&per_page=100')
    def secret_alerts(self,full): return self.req('GET',f'/repos/{full}/secret-scanning/alerts?state=open&per_page=100&hide_secret=true')
    def code_alerts(self,full): return self.req('GET',f'/repos/{full}/code-scanning/alerts?state=open&per_page=100')
    def search_issues(self,full,q): return self.req('GET',f'/search/issues?q={quote(q)}+repo:{quote(full)}&per_page=30').get('items',[])
    def search_commits(self,full,q): return self.req('GET',f'/search/commits?q={quote(q)}+repo:{quote(full)}&per_page=30').get('items',[])
    def search_repositories(self,q): return self.req('GET',f'/search/repositories?q={quote(q)}+user:{quote(self.me().get("login"))}&per_page=30').get('items',[])

_GH_CLIENTS = {k: GH(v['token']) for k,v in GITHUB_ACCOUNTS.items() if v.get('token')}

def active_account_key(c=None, uid=None):
    if c is not None:
        k=c.user_data.get('github_account')
        if k in GITHUB_ACCOUNTS: return k
    if uid is not None:
        k=state_for(uid).get('github_account')
        if k in GITHUB_ACCOUNTS: return k
    return _ACTIVE_GH_ACCOUNT.get()

def activate_account(c=None, uid=None, key=None):
    k=key or active_account_key(c,uid)
    if k not in GITHUB_ACCOUNTS or not GITHUB_ACCOUNTS[k].get('token'): k='1'
    _ACTIVE_GH_ACCOUNT.set(k)
    if c is not None: c.user_data['github_account']=k
    return k

def active_account():
    k=_ACTIVE_GH_ACCOUNT.get()
    return GITHUB_ACCOUNTS.get(k) or GITHUB_ACCOUNTS['1']

def active_token(): return active_account().get('token','')
def active_owner(): return active_account().get('owner','')

class GHProxy:
    def __bool__(self): return bool(active_token())
    def __getattr__(self,name):
        k=_ACTIVE_GH_ACCOUNT.get(); cli=_GH_CLIENTS.get(k)
        if not cli: raise GHError(f'Token untuk {GITHUB_ACCOUNTS.get(k,{}).get("alias",k)} belum dikonfigurasi')
        return getattr(cli,name)

gh = GHProxy()

def esc(x): return html.escape(str(x or '—'))
def kb(rows): return M(rows)
def btn(text,data): return B(text,callback_data=data)
def repo_of(c):
    r=c.user_data.get('repo')
    if r: return r
    uid=getattr(getattr(c,'_user_id',None),'id',None)
    return r
def admin(update): return bool(update.effective_user and ADMINS and update.effective_user.id in ADMINS)
def uid_of(update): return update.effective_user.id if update.effective_user else 0

def remember_repo(update,c,full):
    c.user_data['repo']=full
    c.user_data['fm_dir']='.'
    c.user_data.pop('fm_items',None); c.user_data.pop('fm_file',None)
    uid=uid_of(update); st=state_for(uid); recent=[full]+[x for x in st.get('recent',[]) if x!=full]
    state_patch(uid,repo=full,recent=recent[:10])

def favorite_toggle(uid,full):
    st=state_for(uid); fav=list(st.get('favorites',[]))
    if full in fav: fav.remove(full); on=False
    else: fav.insert(0,full); fav=fav[:30]; on=True
    state_patch(uid,favorites=fav); return on

def fmt_bytes(n):
    n=float(n or 0)
    for unit in ('B','KB','MB','GB'):
        if n<1024: return f'{n:.1f} {unit}'
        n/=1024
    return f'{n:.1f} TB'

async def deny(update):
    if update.callback_query: await update.callback_query.answer('⛔ Akses ditolak.',show_alert=True)
    elif update.effective_message: await update.effective_message.reply_text('⛔ Akses ditolak.')

async def edit_or_send(update,text,markup=None):
    if update.callback_query:
        q=update.callback_query
        try: await q.edit_message_text(text,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)
        except Exception: await q.message.reply_text(text,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)
    else: await update.effective_message.reply_text(text,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)

async def start(update:Update,c:ContextTypes.DEFAULT_TYPE):
    if not admin(update): return await deny(update)
    st=state_for(uid_of(update))
    if st.get('repo'): c.user_data['repo']=st['repo']
    await home(update,c)

async def home(update,c):
    uid=uid_of(update); k=activate_account(c,uid); who='belum terhubung'
    if gh:
        try: who=(await asyncio.to_thread(gh.me)).get('login','?')
        except Exception as e: who=f'error: {e}'
    st=state_for(uid); fav=len(st.get('favorites',[])); recent=len(st.get('recent',[])); a=GITHUB_ACCOUNTS[k]
    text=(f'<b>GitHub Control Center Pro</b>\n\nAkun aktif: <b>{esc(a.get("alias"))}</b>\n'
          f'GitHub login: <code>{esc(who)}</code>\nOwner: <code>{esc(a.get("owner") or "—")}</code>\n'
          f'Repo aktif: <code>{esc(repo_of(c) or "belum dipilih")}</code>\n'
          f'⭐ Favorites: <b>{fav}</b>   🕘 Recent: <b>{recent}</b>\n\nPilih menu:')
    rows=[[btn('🔁 Ganti Akun GitHub','accountswitch')],[btn('📦 Repository','repos:0'),btn('⭐ Favorites','favorites')],[btn('🕘 Recent','recent'),btn('➕ Buat Repo','ask:createrepo')],[btn('📊 Dashboard','health'),btn('🔎 Search','searchmenu')],[btn('📚 Batch Ops','batch'),btn('🔔 Notifications','notifycfg')],[btn('🛡 Security','security'),btn('🧾 Audit','audit')],[btn('🔑 Token/API','tokenhealth'),btn('⚙️ Status Bot','botstatus')],[btn('🔄 Refresh','home')]]
    await edit_or_send(update,text,kb(rows))

async def account(update,c):
    if not gh: return await edit_or_send(update,'❌ <b>GITHUB_TOKEN belum diisi.</b>',kb([[btn('← Kembali','home')]]))
    try: me=await asyncio.to_thread(gh.me)
    except Exception as e: return await show_error(update,e)
    t=f'<b>Akun GitHub</b>\n\nLogin: <code>{esc(me.get("login"))}</code>\nNama: {esc(me.get("name"))}\nPublic repo: {me.get("public_repos",0)}\nFollowers: {me.get("followers",0)}\nID: <code>{me.get("id")}</code>'
    await edit_or_send(update,t,kb([[btn('📦 Repository','repos:0'),btn('← Home','home')]]))

async def account_switch_panel(update,c):
    uid=uid_of(update); activate_account(c,uid)
    cur=active_account_key(c,uid); rows=[]; lines=[]
    for k in ('1','2'):
        a=GITHUB_ACCOUNTS[k]; configured=bool(a.get('token')); owner=a.get('owner') or '—'
        status='✅ aktif' if k==cur else ('🟢 siap' if configured else '⚪ belum diisi')
        lines.append(f'<b>{esc(a.get("alias"))}</b> — {status}\nOwner: <code>{esc(owner)}</code>')
        rows.append([btn(('✅ ' if k==cur else '')+a.get('alias',f'Akun {k}'),f'accountset:{k}')])
    rows.append([btn('← Home','home')])
    await edit_or_send(update,'<b>Pilih Akun GitHub</b>\n\n'+'\n\n'.join(lines)+'\n\nToken tidak pernah ditampilkan atau disimpan di state.',kb(rows))

async def switch_account(update,c,key):
    uid=uid_of(update)
    if key not in GITHUB_ACCOUNTS or not GITHUB_ACCOUNTS[key].get('token'):
        return await edit_or_send(update,f'❌ <b>{esc(GITHUB_ACCOUNTS.get(key,{}).get("alias",key))}</b> belum memiliki token di <code>.env</code>.',kb([[btn('← Akun','accountswitch')]]))
    st=_load_state(); u=st.setdefault(str(uid),{}); old=str(u.get('github_account') or c.user_data.get('github_account') or '1')
    for fld in ('favorites','recent','batch_repos','notify','notify_snapshot','notify_repo'):
        if fld in u: u[f'{fld}_{old}']=u.get(fld)
    for fld in ('favorites','recent','batch_repos','notify','notify_snapshot','notify_repo'):
        keyn=f'{fld}_{key}'
        if keyn in u: u[fld]=u[keyn]
        else: u.pop(fld,None)
    u['github_account']=key; u.pop('repo',None); _save_state(st)
    c.user_data.clear(); c.user_data['github_account']=key; _ACTIVE_GH_ACCOUNT.set(key); cache_clear()
    me=await asyncio.to_thread(gh.me)
    audit(uid,None,'switch_github_account','ok',f'{old}->{key}:{me.get("login")}')
    return await edit_or_send(update,f'✅ Akun aktif: <b>{esc(GITHUB_ACCOUNTS[key]["alias"])}</b>\nLogin GitHub: <code>{esc(me.get("login"))}</code>\nOwner default: <code>{esc(GITHUB_ACCOUNTS[key].get("owner") or "—")}</code>',kb([[btn('📦 Pilih Repository','repos:0')],[btn('← Home','home')]]))

async def repos(update,c,page=0):
    if not gh: return await edit_or_send(update,'❌ Isi <code>GITHUB_TOKEN</code> terlebih dahulu.',kb([[btn('← Home','home')]]))
    try:
        cache_key='repos:'+active_account_key(c)
        rs=cache_get(cache_key)
        if rs is None: rs=cache_set(cache_key,await asyncio.to_thread(gh.repos))
    except Exception as e: return await show_error(update,e)
    c.user_data['repos']=[r['full_name'] for r in rs]
    per=8; start=page*per; chunk=rs[start:start+per]; rows=[]
    for i,r in enumerate(chunk,start): rows.append([btn(('🔒 ' if r.get('private') else '🌐 ')+r['full_name'][:42],f'rsel:{i}')])
    nav=[]
    if page>0: nav.append(btn('‹',f'repos:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(len(rs)+per-1)//per)}','noop'))
    if start+per<len(rs): nav.append(btn('›',f'repos:{page+1}'))
    rows += [nav,[btn('🔄 Muat ulang','reposrefresh'),btn('← Home','home')]]
    await edit_or_send(update,f'<b>Repository</b>\nDitemukan: <b>{len(rs)}</b>\nPilih repository yang ingin dikontrol.',kb(rows))

async def favorites(update,c):
    st=state_for(uid_of(update)); fav=st.get('favorites',[]); c.user_data['quick_repos']=fav
    rows=[[btn('⭐ '+x[:42],f'qrepo:{i}')] for i,x in enumerate(fav[:20])]
    rows.append([btn('← Home','home')])
    await edit_or_send(update,'<b>Favorite Repositories</b>\n\n'+('Pilih repository.' if fav else 'Belum ada favorite.'),kb(rows))

async def recent(update,c):
    st=state_for(uid_of(update)); xs=st.get('recent',[]); c.user_data['quick_repos']=xs
    rows=[[btn('🕘 '+x[:42],f'qrepo:{i}')] for i,x in enumerate(xs[:20])]
    rows.append([btn('← Home','home')])
    await edit_or_send(update,'<b>Recent Repositories</b>\n\n'+('Pilih repository.' if xs else 'Belum ada riwayat repository.'),kb(rows))

async def token_health(update,c):
    me=await asyncio.to_thread(gh.me); rl=await asyncio.to_thread(gh.rate_limit)
    core=(rl.get('resources') or {}).get('core',{})
    search=(rl.get('resources') or {}).get('search',{})
    perm='—'; full=repo_of(c)
    if full:
        try:
            rp=await asyncio.to_thread(gh.repo,full); ps=rp.get('permissions') or {}; perm=' / '.join(k for k in ('admin','maintain','push','triage','pull') if ps.get(k)) or 'read-only/unknown'
        except Exception as e: perm='tidak dapat diuji'
    t=(f'<b>Token & API Health</b>\n\nToken: ✅ Valid\nLogin: <code>{esc(me.get("login"))}</code>\n'
       f'Repo permission aktif: <code>{esc(perm)}</code>\n'
       f'Core API: <b>{core.get("remaining","?")}/{core.get("limit","?")}</b> tersisa\n'
       f'Search API: <b>{search.get("remaining","?")}/{search.get("limit","?")}</b> tersisa\n'
       f'Core reset epoch: <code>{core.get("reset","?")}</code>\n\nNilai token tidak pernah ditampilkan.')
    await edit_or_send(update,t,kb([[btn('🔄 Refresh','tokenhealth'),btn('← Home','home')]]))

async def audit_panel(update,c):
    lines=[]
    if AUDIT_FILE.exists():
        for raw in AUDIT_FILE.read_text(encoding='utf-8',errors='ignore').splitlines()[-15:]:
            try:
                x=json.loads(raw); lines.append(f'• <code>{esc(x.get("ts","")[:19])}</code> {esc(x.get("action"))} — {esc(x.get("repo") or "-")} [{esc(x.get("result"))}]')
            except Exception: pass
    await edit_or_send(update,'<b>Audit Log Bot</b>\n\n'+('\n'.join(lines) or 'Belum ada aktivitas tercatat.'),kb([[btn('🌐 GitHub Activity','activity'),btn('🔄 Refresh','audit')],[btn('← Home','home')]]))

async def activity_panel(update,c):
    full=repo_of(c)
    if not full: return await edit_or_send(update,'Pilih repository terlebih dahulu.',kb([[btn('📦 Repository','repos:0')]]))
    try: xs=await asyncio.to_thread(gh.activity,full)
    except Exception as e: return await show_error(update,e,'audit')
    lines=[]
    for x in xs[:20]:
        actor=((x.get('actor') or {}).get('login') or '—'); ref=x.get('ref') or '—'; typ=x.get('activity_type') or 'activity'
        lines.append(f'• <code>{esc((x.get("timestamp") or "")[:19])}</code> {esc(typ)} — <code>{esc(actor)}</code> • {esc(ref)}')
    await edit_or_send(update,'<b>GitHub Repository Activity</b>\n<code>'+esc(full)+'</code>\n\n'+('\n'.join(lines) or 'Tidak ada aktivitas.'),kb([[btn('🔄 Refresh','activity'),btn('← Audit','audit')]]))

async def health(update,c):
    full=repo_of(c)
    if not full: return await edit_or_send(update,'Pilih repository terlebih dahulu.',kb([[btn('📦 Pilih Repo','repos:0')]]))
    r=await asyncio.to_thread(gh.repo,full)
    runs=await asyncio.to_thread(gh.runs,full); prs=await asyncio.to_thread(gh.pulls,full); iss=await asyncio.to_thread(gh.issues,full); rels=await asyncio.to_thread(gh.releases,full)
    local='Belum clone'; sync='—'; dirty='—'; last='—'
    p=local_path(c)
    if (p/'.git').exists():
        local='Ada'; dirty=await asyncio.to_thread(safe_git,c,'status','--porcelain')
        dirty='Clean' if dirty=='Selesai.' or not dirty.strip() else 'Dirty'
        try: sync=await asyncio.to_thread(safe_git,c,'status','-sb')
        except Exception: sync='error'
        try: last=await asyncio.to_thread(safe_git,c,'log','-1','--oneline')
        except Exception: last='—'
    run=runs[0] if runs else {}; rel=rels[0] if rels else {}
    t=(f'<b>Repository Health</b>\n<code>{esc(full)}</code>\n\n'
       f'Visibility: <b>{"Private" if r.get("private") else "Public"}</b>\nDefault: <code>{esc(r.get("default_branch"))}</code>\n'
       f'Local clone: <b>{local}</b>   Working tree: <b>{dirty}</b>\n'
       f'Open PR: <b>{len(prs)}</b>   Open Issues: <b>{len(iss)}</b>\n'
       f'Actions terakhir: <b>{esc(run.get("conclusion") or run.get("status") or "—")}</b> — {esc(run.get("name") or "—")}\n'
       f'Release terakhir: <code>{esc(rel.get("tag_name") or "—")}</code>\n'
       f'Latest local commit: <code>{esc(last)}</code>\n\n<b>Sync</b>\n<pre>{esc(sync)}</pre>')
    await edit_or_send(update,t,kb([[btn('🔄 Refresh','health'),btn('🔄 Auto Sync','autosync')],[btn('← Repo','repo')]]))

async def security_panel(update,c):
    full=repo_of(c)
    if not full: return await edit_or_send(update,'Pilih repository terlebih dahulu.',kb([[btn('📦 Pilih Repo','repos:0')]]))
    vals={}
    for name,fn in [('Dependabot',gh.dependabot),('Secret scanning',gh.secret_alerts),('Code scanning',gh.code_alerts)]:
        try: vals[name]=len(await asyncio.to_thread(fn,full))
        except Exception as e: vals[name]=f'N/A ({str(e).split(":",1)[0]})'
    t='<b>Security Dashboard</b>\n\n'+ '\n'.join(f'{k}: <b>{esc(v)}</b>' for k,v in vals.items())
    await edit_or_send(update,t,kb([[btn('🔄 Refresh','security'),btn('← Repo','repo')]]))

async def search_menu(update,c):
    full=repo_of(c)
    t=f'<b>Search</b>\n\nRepo aktif: <code>{esc(full or "belum dipilih")}</code>\nPilih jenis pencarian:'
    await edit_or_send(update,t,kb([[btn('🌐 Unified Search','ask:universalsearch')],[btn('🔎 Code/File','ask:searchcode'),btn('🧾 Issues/PR','ask:searchissues')],[btn('🧬 Commits','ask:searchcommits')],[btn('← Home','home')]]))

async def batch_panel(update,c):
    st=state_for(uid_of(update)); selected=st.get('batch_repos',[])
    t='<b>Batch Operations</b>\n\nRepo terpilih: <b>'+str(len(selected))+'</b>\n'+('\n'.join(f'• <code>{esc(x)}</code>' for x in selected[:10]) if selected else 'Belum ada repo dipilih.')
    rows=[[btn('➕ Tambah Repo Aktif','batchadd'),btn('🧹 Kosongkan','batchclear')],[btn('🏷 Tambah Topic','ask:batchtopic'),btn('📝 Ubah Description','ask:batchdesc')],[btn('⬇️ Pull Semua Lokal','batchpull'),btn('📊 Status Semua','batchstatus')],[btn('← Home','home')]]
    await edit_or_send(update,t,kb(rows))

async def notifications_panel(update,c):
    st=state_for(uid_of(update)); cfg=st.get('notify',{})
    on=lambda k:'✅' if cfg.get(k) else '❌'
    t=(f'<b>GitHub Notifications</b>\n\nRepo aktif: <code>{esc(repo_of(c) or "—")}</code>\n'
       f'Push/main {on("push")}  PR {on("pr")}\nIssue {on("issue")}  Workflow gagal {on("workflow")}\nRelease {on("release")}')
    rows=[[btn('Toggle Push','notify:push'),btn('Toggle PR','notify:pr')],[btn('Toggle Issue','notify:issue'),btn('Toggle Workflow','notify:workflow')],[btn('Toggle Release','notify:release')],[btn('← Home','home')]]
    await edit_or_send(update,t,kb(rows))

async def repo_admin(update,c):
    full=repo_of(c)
    r=await asyncio.to_thread(gh.repo,full)
    t=(f'<b>Repository Administration</b>\n\nRepo: <code>{esc(full)}</code>\nName: <code>{esc(r.get("name"))}</code>\n'
       f'Owner: <code>{esc((r.get("owner") or {}).get("login"))}</code>\nArchived: <b>{"Ya" if r.get("archived") else "Tidak"}</b>')
    rows=[[btn('✏️ Rename Repo','ask:renamerepo'),btn('🔁 Transfer Repo','ask:transferrepo')],[btn('⭐ Favorite','favtoggle'),btn('👥 Collaborators','collabs')],[btn('🛡 Rulesets/Protection','rulesets'),btn('🏷 Labels/Milestones','labels')],[btn('📦 Unarchive' if r.get('archived') else '📦 Archive','unarchive' if r.get('archived') else 'archive1')],[btn('🗑 Delete Repo','deleterepo1')],[btn('← Repo','repo')]]
    await edit_or_send(update,t,kb(rows))

async def collaborators_panel(update,c,page=0):
    xs=await asyncio.to_thread(gh.collaborators,repo_of(c)); per=10; chunk=xs[page*per:(page+1)*per]; lines=[]
    for x in chunk:
        perms=x.get('permissions') or {}; p=next((k for k in ('admin','maintain','push','triage','pull') if perms.get(k)), 'unknown')
        lines.append(f'• <code>{esc(x.get("login"))}</code> — {p}')
    nav=[]
    if page>0: nav.append(btn('‹',f'collabpage:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(len(xs)+per-1)//per)}','noop'))
    if (page+1)*per<len(xs): nav.append(btn('›',f'collabpage:{page+1}'))
    rows=[nav,[btn('➕/♻️ Invite/Permission','ask:collabset')],[btn('🗑 Hapus Collaborator','ask:collabdel')],[btn('🔄 Refresh','collabs'),btn('← Admin','repoadmin')]]
    await edit_or_send(update,'<b>Collaborators</b>\n\n'+('\n'.join(lines) or 'Tidak ada collaborator.'),kb(rows))

async def rulesets_panel(update,c):
    try: xs=await asyncio.to_thread(gh.rulesets,repo_of(c))
    except Exception as e: return await show_error(update,e,'repoadmin')
    c.user_data['rulesets']=xs; lines=[f'#{x.get("id")} • {esc(x.get("name"))} — {esc(x.get("enforcement"))}' for x in xs[:30]]
    await edit_or_send(update,'<b>Branch/Repository Rulesets</b>\n\n'+('\n'.join(lines) or 'Belum ada ruleset.')+'\n\nProtection cepat tersedia untuk branch umum; ruleset kompleks dapat dilihat/dihapus dari bot.',kb([[btn('🛡 Protect Branch','ask:protectbranch'),btn('🔓 Unprotect Branch','ask:unprotectbranch')],[btn('🔍 Detail Ruleset','ask:rulesetdetail'),btn('🗑 Hapus Ruleset','ask:rulesetdel')],[btn('🔄 Refresh','rulesets'),btn('← Admin','repoadmin')]]))

async def labels_panel(update,c,page=0):
    labs=await asyncio.to_thread(gh.labels,repo_of(c)); miles=await asyncio.to_thread(gh.milestones,repo_of(c)); per=8
    lchunk=labs[page*per:(page+1)*per]; mchunk=miles[page*per:(page+1)*per]
    text='<b>Labels</b>\n'+('\n'.join(f'• <code>{esc(x.get("name"))}</code> #{esc(x.get("color"))}' for x in lchunk) or '—')
    text+='\n\n<b>Milestones</b>\n'+('\n'.join(f'#{x.get("number")} • {esc(x.get("title"))} [{esc(x.get("state"))}]' for x in mchunk) or '—')
    total=max(len(labs),len(miles)); nav=[]
    if page>0: nav.append(btn('‹',f'labelpage:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(total+per-1)//per)}','noop'))
    if (page+1)*per<total: nav.append(btn('›',f'labelpage:{page+1}'))
    rows=[nav,[btn('➕ Label','ask:labeladd'),btn('✏️ Edit Label','ask:labeledit')],[btn('🏷 Assign Label','ask:labelassign'),btn('🗑 Label','ask:labeldel')],[btn('➕ Milestone','ask:milestoneadd'),btn('✏️ Edit Milestone','ask:milestoneedit')],[btn('✅ Close Milestone','ask:milestoneclose')],[btn('🔄 Refresh','labels'),btn('← Admin','repoadmin')]]
    await edit_or_send(update,text,kb(rows))

async def repo_panel(update,c):
    full=repo_of(c)
    if not full: return await repos(update,c,0)
    try:
        r=cache_get('repo:'+full)
        if r is None: r=cache_set('repo:'+full,await asyncio.to_thread(gh.repo,full))
    except Exception as e: return await show_error(update,e)
    vis='Private' if r.get('private') else 'Public'
    try:
        tps=cache_get('topics:'+full)
        if tps is None: tps=cache_set('topics:'+full,await asyncio.to_thread(gh.topics,full))
    except Exception: tps=[]
    t=(f'<b>{esc(full)}</b>\n\n'
       f'<b>Description</b>\n{esc(r.get("description") or "(kosong)")}\n\n'
       f'<b>Homepage</b>\n<code>{esc(r.get("homepage") or "(kosong)")}</code>\n\n'
       f'Visibility: <b>{vis}</b>   Archived: <b>{"Ya" if r.get("archived") else "Tidak"}</b>\n'
       f'Default branch: <code>{esc(r.get("default_branch"))}</code>\n'
       f'Language: <code>{esc(r.get("language") or "—")}</code>   Size: <b>{r.get("size",0)} KB</b>\n'
       f'⭐ {r.get("stargazers_count",0)}   🍴 {r.get("forks_count",0)}   Issues: {r.get("open_issues_count",0)}\n'
       f'Topics: {(", ".join(tps) if tps else "—")}\n'
       f'Clone: <code>{esc(r.get("clone_url"))}</code>\n'
       f'Updated: <code>{esc(r.get("updated_at"))}</code>')
    rows=[[btn('📊 Overview/Health','health'),btn('📂 Files & Git','localrepo')],[btn('🧾 Issues & PR','workitems'),btn('🚀 Releases','releases')],[btn('⚡ Actions','actions'),btn('🛡 Security','security')],[btn('⚙️ Repo Settings','settings'),btn('🧰 Administration','repoadmin')],[btn('🔐 Secrets/Vars','secvars'),btn('🔎 Search','searchmenu')],[btn('✏️ Description','ask:desc'),btn('🏷 Topics','topics')],[btn('← Daftar Repo','repos:0'),btn('🏠 Home','home')]]
    await edit_or_send(update,t,kb(rows))

async def metadata(update,c):
    r=await asyncio.to_thread(gh.repo,repo_of(c))
    await edit_or_send(update,f'<b>Metadata</b>\n\nDescription:\n{esc(r.get("description"))}\n\nHomepage:\n{esc(r.get("homepage"))}',kb([[btn('✏️ Description','ask:desc'),btn('🔗 Homepage','ask:homeurl')],[btn('← Repo','repo')]]))

async def topics(update,c):
    names=await asyncio.to_thread(gh.topics,repo_of(c))
    await edit_or_send(update,'<b>Topics</b>\n\n'+(', '.join(f'<code>{esc(x)}</code>' for x in names) or 'Belum ada topic.'),kb([[btn('✏️ Ubah Topics','ask:topics')],[btn('← Repo','repo')]]))

async def workitems(update,c):
    await edit_or_send(update,f'<b>Issues & Pull Requests</b>\n<code>{esc(repo_of(c))}</code>',kb([[btn('🧾 Issues','issues'),btn('🔀 Pull Requests','pulls')],[btn('🏷 Labels/Milestones','labels')],[btn('← Repo','repo')]]))

async def releases(update,c,page=0):
    rs=await asyncio.to_thread(gh.releases,repo_of(c)); per=8; chunk=rs[page*per:(page+1)*per]; c.user_data['release_items']=chunk
    rows=[[btn(f'🚀 {x.get("tag_name","")[:18]} • {x.get("name","")[:25]}',f'reldetail:{i}')] for i,x in enumerate(chunk)]
    nav=[]
    if page>0: nav.append(btn('‹',f'relpage:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(len(rs)+per-1)//per)}','noop'))
    if (page+1)*per<len(rs): nav.append(btn('›',f'relpage:{page+1}'))
    rows += [nav,[btn('➕ Buat Release','ask:release'),btn('📎 Upload Asset','ask:releaseasset')],[btn('🔄 Refresh','releases'),btn('← Repo','repo')]]
    await edit_or_send(update,'<b>Releases</b>\nPilih release untuk detail/edit/delete/assets.',kb(rows))

async def release_detail(update,c,idx):
    xs=c.user_data.get('release_items',[])
    if idx<0 or idx>=len(xs): return await releases(update,c)
    r=xs[idx]; c.user_data['release_id']=r['id']; c.user_data['release_tag']=r.get('tag_name')
    assets=r.get('assets',[])
    text=(f'<b>Release {esc(r.get("tag_name"))}</b>\n\nName: {esc(r.get("name"))}\nDraft: <b>{r.get("draft")}</b>   Prerelease: <b>{r.get("prerelease")}</b>\n'
          f'Published: <code>{esc(r.get("published_at") or "—")}</code>\nAssets: <b>{len(assets)}</b>\n\n{esc((r.get("body") or "")[:1800])}')
    rows=[[btn('✏️ Edit Release','ask:editrelease'),btn('📎 Upload Asset','releaseassetselected')],[btn('🗑 Delete Release','delrelease1')]]
    for i,a in enumerate(assets[:6]): rows.append([btn('🗑 Asset '+a.get('name','')[:35],f'delasset:{a.get("id")}')])
    rows.append([btn('← Releases','releases')]); await edit_or_send(update,text,kb(rows))

async def issues(update,c,page=0):
    xs=await asyncio.to_thread(gh.issues,repo_of(c)); per=8; chunk=xs[page*per:(page+1)*per]; c.user_data['issue_items']=chunk
    rows=[[btn(f'#{x["number"]} {x["title"][:40]}',f'issuedetail:{i}')] for i,x in enumerate(chunk)]
    nav=[]
    if page>0: nav.append(btn('‹',f'issuepage:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(len(xs)+per-1)//per)}','noop'))
    if (page+1)*per<len(xs): nav.append(btn('›',f'issuepage:{page+1}'))
    rows += [nav,[btn('➕ Buat Issue','ask:issue'),btn('✏️ Edit by #','ask:newissueedit')],[btn('🔄 Refresh','issues'),btn('← Issues & PR','workitems')]]
    await edit_or_send(update,'<b>Open Issues</b>\nPilih issue untuk detail.',kb(rows))

async def issue_detail(update,c,idx):
    xs=c.user_data.get('issue_items',[])
    if idx<0 or idx>=len(xs): return await issues(update,c)
    num=xs[idx]['number']; x=await asyncio.to_thread(gh.issue,repo_of(c),num); cm=await asyncio.to_thread(gh.issue_comments,repo_of(c),num)
    c.user_data['issue_num']=num
    labels=', '.join(y.get('name','') for y in x.get('labels',[])) or '—'; ass=', '.join(y.get('login','') for y in x.get('assignees',[])) or '—'
    comments='\n'.join(f'• <code>{esc((z.get("user") or {}).get("login"))}</code>: {esc((z.get("body") or "")[:220])}' for z in cm[-5:]) or '—'
    t=(f'<b>Issue #{num}</b> — {esc(x.get("title"))}\n\nAuthor: <code>{esc((x.get("user") or {}).get("login"))}</code>\nState: <b>{esc(x.get("state"))}</b>\n'
       f'Labels: {esc(labels)}\nAssignees: {esc(ass)}\nMilestone: {esc((x.get("milestone") or {}).get("title") or "—")}\nComments: <b>{len(cm)}</b>\n\n'
       f'<b>Body</b>\n{esc((x.get("body") or "")[:1800])}\n\n<b>Komentar terbaru</b>\n{comments}')
    await edit_or_send(update,t,kb([[btn('✏️ Edit','issueeditselected'),btn('✅ Close','issuecloseselected')],[btn('← Issues','issues')]]))

async def pulls(update,c,page=0):
    xs=await asyncio.to_thread(gh.pulls,repo_of(c)); per=8; chunk=xs[page*per:(page+1)*per]; c.user_data['pull_items']=chunk
    rows=[[btn(f'#{x["number"]} {x["title"][:38]}',f'prdetail:{i}')] for i,x in enumerate(chunk)]
    nav=[]
    if page>0: nav.append(btn('‹',f'prpage:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(len(xs)+per-1)//per)}','noop'))
    if (page+1)*per<len(xs): nav.append(btn('›',f'prpage:{page+1}'))
    rows += [nav,[btn('➕ Buat PR','ask:createpr')],[btn('🔄 Refresh','pulls'),btn('← Issues & PR','workitems')]]
    await edit_or_send(update,'<b>Open Pull Requests</b>\nPilih PR untuk detail/review/files/merge.',kb(rows))

async def pull_detail(update,c,idx):
    xs=c.user_data.get('pull_items',[])
    if idx<0 or idx>=len(xs): return await pulls(update,c)
    num=xs[idx]['number']; x=await asyncio.to_thread(gh.pull,repo_of(c),num); files=await asyncio.to_thread(gh.pull_files,repo_of(c),num); commits=await asyncio.to_thread(gh.pull_commits,repo_of(c),num); reviews=await asyncio.to_thread(gh.reviews,repo_of(c),num)
    try: checks=await asyncio.to_thread(gh.check_runs,repo_of(c),(x.get('head') or {}).get('sha'))
    except Exception: checks=[]
    c.user_data['pr_num']=num
    review_states=', '.join((r.get('state') or '?') for r in reviews[-6:]) or '—'; check_states=', '.join(f'{y.get("name")}:{y.get("conclusion") or y.get("status")}' for y in checks[:8]) or '—'
    t=(f'<b>PR #{num}</b> — {esc(x.get("title"))}\n\nAuthor: <code>{esc((x.get("user") or {}).get("login"))}</code>\n'
       f'Branch: <code>{esc((x.get("head") or {}).get("ref"))} → {esc((x.get("base") or {}).get("ref"))}</code>\n'
       f'Mergeable: <b>{esc(x.get("mergeable"))}</b>   Draft: <b>{x.get("draft")}</b>\nCommits: <b>{len(commits)}</b>   Files: <b>{len(files)}</b>   Reviews: <b>{len(reviews)}</b>\n'
       f'Review states: <code>{esc(review_states)}</code>\nChecks: <code>{esc(check_states)}</code>\n\n'
       + '\n'.join(f'• <code>{esc(f.get("filename"))}</code> +{f.get("additions",0)}/-{f.get("deletions",0)}' for f in files[:12]))
    await edit_or_send(update,t,kb([[btn('✅ Squash Merge','prmerge:squash'),btn('🔀 Merge','prmerge:merge')],[btn('🧬 Rebase Merge','prmerge:rebase'),btn('🚫 Close PR','prcloseselected')],[btn('← Pull Requests','pulls')]]))

async def branches(update,c,page=0):
    xs=await asyncio.to_thread(gh.branches,repo_of(c)); per=12; chunk=xs[page*per:(page+1)*per]
    lines=[('🛡 ' if x.get('protected') else '• ')+f'<code>{esc(x["name"])}</code>' for x in chunk]
    nav=[]
    if page>0: nav.append(btn('‹',f'branchpage:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(len(xs)+per-1)//per)}','noop'))
    if (page+1)*per<len(xs): nav.append(btn('›',f'branchpage:{page+1}'))
    await edit_or_send(update,'<b>Branches</b>\n\n'+('\n'.join(lines) or 'Tidak ada branch.'),kb([nav,[btn('🔄 Refresh','branches'),btn('← Repo','repo')]]))

async def tags_panel(update,c,page=0):
    try:
        xs=await asyncio.to_thread(gh.tags,repo_of(c)); per=12; chunk=xs[page*per:(page+1)*per]
        lines=[f'• <code>{esc(x.get("name"))}</code>' for x in chunk]
        nav=[]
        if page>0: nav.append(btn('‹',f'tagpage:{page-1}'))
        nav.append(btn(f'{page+1}/{max(1,(len(xs)+per-1)//per)}','noop'))
        if (page+1)*per<len(xs): nav.append(btn('›',f'tagpage:{page+1}'))
        rows=[nav,[btn('➕ Buat Tag','ask:newtag'),btn('🗑 Hapus Tag','ask:deltag')],[btn('🔄 Refresh','tags'),btn('← Git / Upload','localrepo')]]
        await edit_or_send(update,'<b>Git Tags</b>\n\n'+('\n'.join(lines) or 'Belum ada tag.'),kb(rows))
    except Exception as e: await show_error(update,e,'localrepo')

async def actions(update,c,page=0):
    runs=await asyncio.to_thread(gh.runs,repo_of(c)); wfs=await asyncio.to_thread(gh.workflows,repo_of(c)); per=6; chunk=runs[page*per:(page+1)*per]; c.user_data['run_items']=chunk
    icons={'success':'✅','failure':'❌','cancelled':'🚫','skipped':'⏭'}
    rows=[[btn(f'{icons.get(x.get("conclusion"),"⏳")} {x.get("name","")[:36]}',f'rundetail:{i}')] for i,x in enumerate(chunk)]
    nav=[]
    if page>0: nav.append(btn('‹',f'actionpage:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(len(runs)+per-1)//per)}','noop'))
    if (page+1)*per<len(runs): nav.append(btn('›',f'actionpage:{page+1}'))
    rows.append(nav); rows += [[btn('▶️ '+w['name'][:38],f'wf:{w["id"]}')] for w in wfs[:6]]; rows.append([btn('🔄 Refresh','actions'),btn('← Repo','repo')])
    await edit_or_send(update,'<b>GitHub Actions</b>\nPilih run untuk detail/rerun/cancel/delete, atau workflow untuk dispatch.',kb(rows))

async def run_detail(update,c,idx):
    xs=c.user_data.get('run_items',[])
    if idx<0 or idx>=len(xs): return await actions(update,c)
    rid=xs[idx]['id']; r=await asyncio.to_thread(gh.run_detail,repo_of(c),rid); jobs=await asyncio.to_thread(gh.jobs,repo_of(c),rid); c.user_data['run_id']=rid
    t=(f'<b>Workflow Run #{r.get("run_number")}</b>\n\nName: {esc(r.get("name"))}\nStatus: <b>{esc(r.get("status"))}</b> / <b>{esc(r.get("conclusion"))}</b>\n'
       f'Branch: <code>{esc(r.get("head_branch"))}</code>\nEvent: <code>{esc(r.get("event"))}</code>\nJobs: <b>{len(jobs)}</b>\n\n'+ '\n'.join(f'• {esc(j.get("name"))}: {esc(j.get("conclusion") or j.get("status"))}' for j in jobs[:15]))
    await edit_or_send(update,t,kb([[btn('📥 Download Logs','runlogs')],[btn('🔄 Rerun','runrerun'),btn('🔁 Failed Jobs','runfailed')],[btn('⏹ Cancel','runcancel'),btn('🗑 Delete Run','rundel1')],[btn('← Actions','actions')]]))

async def secvars(update,c):

    ss=await asyncio.to_thread(gh.secrets,repo_of(c)); vs=await asyncio.to_thread(gh.variables,repo_of(c))
    st=', '.join(f'<code>{esc(x["name"])}</code>' for x in ss) or '—'; vt='\n'.join(f'• <code>{esc(x["name"])}</code> = {esc(x.get("value"))}' for x in vs) or '—'
    await edit_or_send(update,f'<b>Secrets & Variables</b>\n\n<b>Secrets</b> (nilai disembunyikan GitHub):\n{st}\n\n<b>Variables</b>:\n{vt}',kb([[btn('➕/♻️ Secret','ask:secret'),btn('🗑 Secret','ask:delsecret')],[btn('➕/♻️ Variable','ask:var'),btn('🗑 Variable','ask:delvar')],[btn('← Repo','repo')]]))

async def settings(update,c):
    r=await asyncio.to_thread(gh.repo,repo_of(c)); on=lambda x:'✅' if x else '❌'
    t=f'<b>Repository Settings</b>\n\nIssues {on(r.get("has_issues"))}  Wiki {on(r.get("has_wiki"))}\nProjects {on(r.get("has_projects"))}  Discussions {on(r.get("has_discussions"))}\nDelete branch after merge {on(r.get("delete_branch_on_merge"))}\nArchived {on(r.get("archived"))}'
    rows=[[btn('Toggle Issues','tog:has_issues'),btn('Toggle Wiki','tog:has_wiki')],[btn('Toggle Projects','tog:has_projects'),btn('Toggle Discussions','tog:has_discussions')],[btn('Toggle Auto-delete Branch','tog:delete_branch_on_merge')],[btn('📌 Default Branch','ask:defbranch'),btn('👁 Visibility','ask:visibility')],[btn('📦 Archive Repo','archive1')],[btn('← Repo','repo')]]
    await edit_or_send(update,t,kb(rows))

async def local_panel(update,c):
    full=repo_of(c)
    if not full: return await edit_or_send(update,'Pilih repository terlebih dahulu.',kb([[btn('📦 Pilih Repo','repos:0')],[btn('← Home','home')]]))
    path=local_path(c); exists=(path/'.git').exists()
    t=(f'<b>Files & Git</b>\n\nRepo: <code>{esc(full)}</code>\nPath: <code>{esc(path)}</code>\n'
       f'Status clone: <b>{"Ada" if exists else "Belum ada — akan auto-clone saat fitur file dipakai"}</b>')
    rows=[
        [btn('📂 File & Folder Manager','files')],
        [btn('📁 Buat Folder','ask:newfolder'),btn('📝 Buat File','ask:createfilepath')],
        [btn('📤 Upload File','fmmulti'),btn('🖼 Upload Media','fmmedia')],
        [btn('📦 Upload & Extract ZIP','fmzip')],
    ]
    if not exists:
        rows.append([btn('📥 Clone Repository Sekarang','clone')])
    else:
        rows += [
            [btn('📂 File Status','gitstatus'),btn('🧾 Diff','gitdiff')],
            [btn('📜 Log','gitlog'),btn('🔎 Commit Detail','ask:showcommit')],
            [btn('⬇️ Pull','gitpull'),btn('📡 Fetch','gitfetch')],
            [btn('✅ Commit','ask:commit'),btn('✅ Commit + Push','ask:commitpush')],
            [btn('⬆️ Push','gitpush'),btn('🌿 Branch Lokal','gitbranch')],
            [btn('➕ Branch Baru','ask:newbranch'),btn('🔀 Checkout','ask:checkout')],
            [btn('📦 Stash','gitstash'),btn('↩️ Stash Pop','gitstashpop')],
            [btn('🍒 Cherry-pick','ask:cherrypick'),btn('↩️ Revert Commit','ask:revert')],
            [btn('⚖️ Compare Ref','ask:compare'),btn('🏷 Tags','tags')],
            [btn('🧩 Selective Commit','ask:selectcommit'),btn('📄 README Editor','readme')],
            [btn('🧰 Repo Template','ask:template'),btn('🩹 Conflict Helper','conflicts')],
            [btn('🔄 Auto Sync','autosync'),btn('🛟 Safety Backup','safetybackup')],
            [btn('⚠️ Reset Terkontrol','ask:reset')],
        ]
    rows.append([btn('← Repo','repo')]); await edit_or_send(update,t,kb(rows))


async def ensure_local_repo(update,c,announce=False):
    dest=local_path(c)
    if (dest/'.git').exists(): return dest
    if dest.exists() and any(dest.iterdir()): raise ValueError(f'Folder lokal sudah ada tetapi bukan clone Git yang valid: {dest}')
    full=repo_of(c)
    if not full: raise ValueError('Pilih repository terlebih dahulu')
    dest.parent.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy(); tok=active_token(); env['GH_TOKEN']=tok; env['GITHUB_TOKEN']=tok
    if announce: await edit_or_send(update,f'⏳ Auto-clone <code>{esc(full)}</code>...',None)
    r=await asyncio.to_thread(subprocess.run,['gh','repo','clone',full,str(dest)],capture_output=True,text=True,timeout=180,env=env)
    if r.returncode: raise RuntimeError((r.stderr or r.stdout)[-1500:])
    audit(uid_of(update),full,'auto_clone','ok',str(dest))
    return dest


def local_path(c):
    full=repo_of(c)
    if not full: return LOCAL_BASE/'_none'
    k=active_account_key(c)
    if k=='1': return LOCAL_BASE/full.split('/')[-1]
    owner,name=(full.split('/',1)+[''])[:2]
    safe_owner=re.sub(r'[^A-Za-z0-9_.-]','_',owner)
    return LOCAL_BASE/f'account-{k}'/safe_owner/name
def safe_git(c,*args):
    p=local_path(c)
    if not str(p).startswith(str(LOCAL_BASE)+os.sep): raise RuntimeError('Path repo tidak aman')
    env=os.environ.copy(); tok=active_token(); env['GH_TOKEN']=tok; env['GITHUB_TOKEN']=tok
    r=subprocess.run(['git','-c','credential.helper=!gh auth git-credential','-C',str(p),*args],capture_output=True,text=True,timeout=120,env=env)
    if r.returncode: raise RuntimeError((r.stderr or r.stdout)[-1800:])
    return (r.stdout or 'Selesai.').strip()[-3500:]

async def do_git(update,c,kind):
    try:
        mapping={
            'status':('status','--short','--branch'),
            'log':('log','-12','--oneline','--decorate','--graph'),
            'diff':('diff','--stat','HEAD'),
            'pull':('pull','--ff-only'),
            'fetch':('fetch','--all','--prune'),
            'push':('push',),
            'branch':('branch','-vv'),
        }
        out=await asyncio.to_thread(safe_git,c,*mapping[kind]); await edit_or_send(update,f'<b>Git {kind}</b>\n<pre>{esc(out)}</pre>',kb([[btn('← Git / Upload','localrepo')]]))
    except Exception as e: await show_error(update,e,'localrepo')

async def clone(update,c):
    full=repo_of(c); dest=local_path(c)
    if dest.exists(): return await edit_or_send(update,'Folder tujuan sudah ada.',kb([[btn('← Git Lokal','localrepo')]]))
    dest.parent.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy(); tok=active_token(); env['GH_TOKEN']=tok; env['GITHUB_TOKEN']=tok
    try:
        r=await asyncio.to_thread(subprocess.run,['gh','repo','clone',full,str(dest)],capture_output=True,text=True,timeout=180,env=env)
        if r.returncode: raise RuntimeError(r.stderr[-1500:])
        await edit_or_send(update,f'✅ Clone selesai.\n<code>{esc(dest)}</code>',kb([[btn('← Git Lokal','localrepo')]]))
    except Exception as e: await show_error(update,e,'localrepo')

def safe_repo_path(c, rel='.'):
    root=local_path(c).resolve(); target=(root/rel).resolve()
    if target != root and not str(target).startswith(str(root)+os.sep): raise ValueError('Path keluar repository tidak diizinkan')
    return root,target

async def file_manager(update,c,rel=None,page=0):
    await ensure_local_repo(update,c,False)
    root=local_path(c).resolve()
    if rel is None: rel=c.user_data.get('fm_dir','.')
    try:
        _,probe=safe_repo_path(c,rel)
        if not probe.exists() or not probe.is_dir(): rel='.'
    except Exception:
        rel='.'
    _,cur=safe_repo_path(c,rel); c.user_data['fm_dir']=str(cur.relative_to(root)) if cur!=root else '.'
    items=[]
    for p in sorted(cur.iterdir(), key=lambda x:(not x.is_dir(),x.name.lower())):
        if p.name=='.git': continue
        items.append(p)
    per=10; chunk=items[page*per:(page+1)*per]; c.user_data['fm_items']=[str(x) for x in chunk]
    rows=[]
    for i,p in enumerate(chunk):
        icon='📁' if p.is_dir() else '📄'
        rows.append([btn(f'{icon} {p.name[:45]}',f'fmopen:{i}')])
    nav=[]
    if page>0: nav.append(btn('‹',f'fmpage:{page-1}'))
    nav.append(btn(f'{page+1}/{max(1,(len(items)+per-1)//per)}','noop'))
    if (page+1)*per<len(items): nav.append(btn('›',f'fmpage:{page+1}'))
    if nav: rows.append(nav)
    extra=[[btn('📁 Buat Folder','ask:newfolder'),btn('📝 Buat File','ask:createfilepath')],[btn('📤 Upload File','fmmulti'),btn('🖼 Upload Media','fmmedia')],[btn('📦 Upload & Extract ZIP','fmzip')]]
    if cur!=root: extra.append([btn('⬆️ Folder Atas','fmup')])
    extra.append([btn('← Git / Upload','localrepo')])
    rows += extra
    reltxt=c.user_data['fm_dir']; crumb=repo_of(c)+' › Files'+((' › '+reltxt.replace('/',' › ')) if reltxt!='.' else '')
    await edit_or_send(update,f'<b>File Manager</b>\n\n<code>{esc(crumb)}</code>\nFolder: <code>{esc(reltxt)}</code>\nItem: <b>{len(items)}</b>',kb(rows))

async def file_detail(update,c,idx):
    paths=c.user_data.get('fm_items',[])
    if idx<0 or idx>=len(paths): return await file_manager(update,c)
    p=Path(paths[idx]); root=local_path(c).resolve()
    if p.is_dir(): return await file_manager(update,c,str(p.relative_to(root)),0)
    c.user_data['fm_file']=str(p)
    size=p.stat().st_size
    text=f'<b>File</b>\n\nPath: <code>{esc(p.relative_to(root))}</code>\nUkuran: <b>{size}</b> byte'
    rows=[[btn('⬇️ Download','fmdownload'),btn('✏️ Rename','ask:renamefile')],[btn('🗑 Delete','fmdelete1')],[btn('← File Manager','files')]]
    await edit_or_send(update,text,kb(rows))

async def send_current_file(update,c):
    p=Path(c.user_data.get('fm_file',''))
    root=local_path(c).resolve()
    if not p.exists() or not p.is_file() or (p!=root and not str(p.resolve()).startswith(str(root)+os.sep)): return await show_error(update,'File tidak ditemukan','files')
    if p.stat().st_size>49*1024*1024: return await show_error(update,'File terlalu besar untuk dikirim bot (>49MB)','files')
    await update.callback_query.message.reply_document(document=p.open('rb'),filename=p.name,caption=str(p.relative_to(root)))

async def safety_backup(update,c,announce=True):
    p=local_path(c)
    if not (p/'.git').exists(): raise ValueError('Repository belum di-clone')
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S'); name=f'bot-backup/{stamp}'
    await asyncio.to_thread(safe_git,c,'branch',name,'HEAD')
    audit(uid_of(update),repo_of(c),'safety_backup','ok',name)
    if announce: await edit_or_send(update,f'✅ Safety branch dibuat: <code>{esc(name)}</code>',kb([[btn('← Git / Upload','localrepo')]]))
    return name

async def conflict_helper(update,c):
    try: out=await asyncio.to_thread(safe_git,c,'diff','--name-only','--diff-filter=U')
    except Exception as e: return await show_error(update,e,'localrepo')
    files=[x for x in out.splitlines() if x and x!='Selesai.']
    state=[]
    p=local_path(c)
    for marker,label in [('.git/MERGE_HEAD','merge'),('.git/CHERRY_PICK_HEAD','cherry-pick'),('.git/REVERT_HEAD','revert'),('.git/rebase-merge','rebase'),('.git/rebase-apply','rebase')]:
        if (p/marker).exists(): state.append(label)
    t='<b>Conflict Helper</b>\n\nOperation: <code>'+esc(', '.join(dict.fromkeys(state)) or 'none')+'</code>\nConflicted files: <b>'+str(len(files))+'</b>\n'+('\n'.join(f'• <code>{esc(x)}</code>' for x in files[:30]) if files else 'Tidak ada conflict terdeteksi.')
    rows=[[btn('▶️ Continue','confcontinue'),btn('⛔ Abort','confabort')],[btn('📂 File Manager','files'),btn('← Git','localrepo')]]
    await edit_or_send(update,t,kb(rows))

async def readme_panel(update,c):
    root=local_path(c); p=root/'README.md'
    current='(README.md belum ada)'
    if p.exists():
        try: current=p.read_text(encoding='utf-8',errors='replace')[:3000]
        except Exception: current='(tidak dapat dibaca sebagai teks)'
    await edit_or_send(update,f'<b>README Editor</b>\n\n<pre>{esc(current)}</pre>',kb([[btn('✏️ Edit README','ask:readmeedit')],[btn('← Git / Upload','localrepo')]]))

async def auto_sync(update,c):
    try:
        status=await asyncio.to_thread(safe_git,c,'status','--porcelain')
        if status!='Selesai.' and status.strip():
            return await edit_or_send(update,'⚠️ Working tree kotor. Auto Sync dibatalkan agar perubahan lokal tidak tertimpa.',kb([[btn('📂 Status','gitstatus'),btn('← Git','localrepo')]]))
        msg=await edit_or_send(update,'⏳ <b>Auto Sync</b>\n1/3 Fetch...',None)
        await asyncio.to_thread(safe_git,c,'fetch','--all','--prune')
        await asyncio.to_thread(safe_git,c,'pull','--ff-only')
        out=await asyncio.to_thread(safe_git,c,'status','-sb')
        audit(uid_of(update),repo_of(c),'auto_sync','ok',out)
        await edit_or_send(update,f'✅ <b>Auto Sync selesai</b>\n\n<pre>{esc(out)}</pre>',kb([[btn('← Git','localrepo')]]))
    except Exception as e: await show_error(update,e,'localrepo')

async def diff_menu(update,c):
    try:
        status=await asyncio.to_thread(safe_git,c,'status','--short')
        unstaged=await asyncio.to_thread(safe_git,c,'diff','--stat')
        staged=await asyncio.to_thread(safe_git,c,'diff','--cached','--stat')
        t=(f'<b>Diff Center</b>\n\n<b>Status</b>\n<pre>{esc(status)}</pre>\n\n'
           f'<b>Unstaged</b>\n<pre>{esc(unstaged)}</pre>\n\n<b>Staged</b>\n<pre>{esc(staged)}</pre>')
        rows=[[btn('📄 Diff per File','ask:diffpath'),btn('🧩 Selective Commit','ask:selectcommit')],[btn('↩️ Discard File','ask:discardfile')],[btn('← Git / Upload','localrepo')]]
        await edit_or_send(update,t,kb(rows))
    except Exception as e: await show_error(update,e,'localrepo')

async def apply_template(update,c,kind):
    root=local_path(c)
    if not (root/'.git').exists(): raise ValueError('Repository belum di-clone')
    name=repo_of(c).split('/')[-1]
    mit=('MIT License\n\nCopyright (c) 2026\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\n'
         'of this software and associated documentation files (the "Software"), to deal\nin the Software without restriction, including without limitation the rights\n'
         'to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\n'
         'furnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\n'
         'copies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
         'IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n'
         'AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n'
         'OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n')
    common={'README.md':f'# {name}\n\nProject repository.\n','LICENSE':mit,'.env.example':'# Copy to .env and fill values locally.\n','src/.gitkeep':'','docs/.gitkeep':''}
    if kind=='python':
        files={**common,'.gitignore':'__pycache__/\n*.py[cod]\n.venv/\n.env\n.pytest_cache/\ndist/\nbuild/\n','requirements.txt':'','tests/.gitkeep':''}
    elif kind=='node':
        files={**common,'.gitignore':'node_modules/\n.env\ndist/\ncoverage/\n*.log\n','package.json':json.dumps({'name':name.lower().replace('_','-'),'version':'1.0.0','private':True,'scripts':{'test':'echo "add tests"'}},indent=2)+'\n'}
    elif kind=='generic':
        files={**common,'.gitignore':'.env\n*.log\n.DS_Store\nThumbs.db\n'}
    elif kind=='github-actions':
        files={'.github/workflows/ci.yml':'name: CI\non:\n  push:\n  pull_request:\njobs:\n  check:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - name: Validate\n        run: echo "Configure project-specific CI steps"\n'}
    else: raise ValueError('Template harus python/node/generic/github-actions')
    made=[]
    for rel,content in files.items():
        p=root/rel; p.parent.mkdir(parents=True,exist_ok=True)
        if not p.exists(): p.write_text(content,encoding='utf-8'); made.append(rel)
    if kind in ('python','node','generic'):
        try:
            existing=await asyncio.to_thread(safe_git,c,'branch','--list','develop')
            if existing=='Selesai.' or not existing.strip(): await asyncio.to_thread(safe_git,c,'branch','develop','HEAD'); made.append('branch:develop')
        except Exception: pass
    audit(uid_of(update),repo_of(c),'apply_template','ok',','.join(made))
    return made

PROMPTS={
    'desc':'Kirim <b>description</b> baru. Kirim <code>-</code> untuk menghapus.',
    'homeurl':'Kirim URL homepage. Kirim <code>-</code> untuk menghapus.',
    'topics':'Kirim topics dipisahkan koma. Contoh: <code>python, telegram-bot, automation</code>',
    'release':'Format: <code>tag | judul | catatan</code>\nCatatan boleh dikosongkan untuk auto release notes.',
    'releaseasset':'Kirim tag release tujuan, contoh <code>v1.2.0</code>. Setelah itu kirim file asset sebagai Document.',
    'issue':'Format: <code>judul | isi issue</code>',
    'secret':'Format: <code>NAMA_SECRET | nilai</code>\nPesan Anda akan dihapus setelah diproses.',
    'delsecret':'Kirim nama secret yang akan dihapus.',
    'var':'Format: <code>NAMA_VARIABLE | nilai</code>',
    'delvar':'Kirim nama variable yang akan dihapus.',
    'defbranch':'Kirim nama default branch baru.',
    'visibility':'Kirim salah satu: <code>public</code> atau <code>private</code>.',
    'commit':'Kirim commit message. Semua perubahan akan di-stage dengan <code>git add -A</code>.',
    'commitpush':'Kirim commit message. Bot akan <b>stage → commit → push</b> ke branch aktif.',
    'newbranch':'Kirim nama branch baru. Contoh: <code>feature/upload-bot</code>.',
    'checkout':'Kirim nama branch yang ingin di-checkout.',
    'showcommit':'Kirim SHA/ref commit yang ingin dilihat, misalnya <code>HEAD</code> atau <code>a1b2c3d</code>.',
    'cherrypick':'Kirim SHA commit yang ingin di-cherry-pick.',
    'revert':'Kirim SHA commit yang ingin direvert. Bot akan membuat revert commit baru.',
    'compare':'Format: <code>base | head</code>, contoh <code>main | feature-x</code>.',
    'newtag':'Format: <code>v1.2.0 | pesan tag</code>. Pesan boleh kosong.',
    'deltag':'Kirim nama tag yang ingin dihapus dari lokal dan remote.',
    'searchcode':'Kirim kata/teks yang ingin dicari di repository GitHub.',
    'newtext':'Format: <code>path/file.txt | isi file</code>. File dibuat/ditimpa di working tree lokal.',
    'renamefile':'Kirim path/nama baru relatif terhadap repo.',
    'newissueedit':'Format: <code>nomor | judul baru | isi baru</code>. Judul/isi boleh dikosongkan untuk mempertahankan.',
    'closeissue':'Kirim nomor issue yang ingin ditutup.',
    'createpr':'Format: <code>judul | head | base | isi</code>. Contoh: <code>Update | feature-x | main | Keterangan</code>.',
    'closepr':'Kirim nomor PR yang ingin ditutup.',
    'mergepr':'Format: <code>nomor | merge/squash/rebase</code>. Default squash.',
    'reset':'Format: <code>ref | soft/mixed/hard</code>. Contoh <code>HEAD~1 | hard</code>. Semua mode meminta konfirmasi.',
    'uploadpath':'Kirim path tujuan di dalam repo, misalnya <code>scripts/app.py</code> atau <code>README.md</code>. Setelah itu kirim file sebagai dokumen Telegram.',
    'createrepo':'Format: <code>nama | public/private | description</code>. Repo dibuat dengan README awal.',
    'renamerepo':'Kirim nama repository baru.',
    'collabset':'Format: <code>username | pull/triage/push/maintain/admin</code>.',
    'collabdel':'Kirim username collaborator yang akan dihapus.',
    'rulesetdetail':'Kirim ID ruleset yang ingin dilihat.',
    'rulesetdel':'Kirim ID ruleset yang ingin dihapus.',
    'labeladd':'Format: <code>nama | hexcolor | description</code>, contoh <code>bug | d73a4a | Bug</code>.',
    'labeldel':'Kirim nama label yang ingin dihapus.',
    'milestoneadd':'Format: <code>judul | description</code>.',
    'milestoneclose':'Kirim nomor milestone yang ingin ditutup.',
    'editrelease':'Format: <code>judul | body | draft true/false | prerelease true/false</code>. Kosongkan field untuk mempertahankan nilai.',
    'searchissues':'Kirim kata/teks untuk mencari issue dan PR pada repo aktif.',
    'searchcommits':'Kirim kata/teks untuk mencari commit pada repo aktif.',
    'batchtopic':'Kirim satu topic yang akan ditambahkan ke seluruh repo batch.',
    'selectcommit':'Format: <code>file1,file2 | pesan commit</code>. Hanya file terpilih yang di-stage dan commit.',
    'readmeedit':'Kirim isi README.md baru. Bot akan menimpa README lokal lalu menawarkan diff/commit.',
    'template':'Kirim pilihan template: <code>python</code>, <code>node</code>, <code>generic</code>, atau <code>github-actions</code>.',
    'diffpath':'Kirim path file untuk melihat diff lengkap file tersebut.',
    'discardfile':'Kirim path file yang perubahan lokalnya akan dibuang. Bot akan membuat safety backup lebih dulu.',
    'transferrepo':'Format: <code>owner_baru | nama_baru_opsional</code>. Transfer repository memerlukan izin GitHub yang sesuai.',
    'protectbranch':'Format: <code>branch | jumlah_review</code>. Contoh <code>main | 1</code>.',
    'unprotectbranch':'Kirim nama branch yang protection-nya akan dihapus.',
    'labeledit':'Format: <code>nama_lama | nama_baru | hexcolor | description</code>. Field setelah nama lama boleh dikosongkan.',
    'labelassign':'Format: <code>nomor_issue_atau_PR | label1,label2</code>.',
    'milestoneedit':'Format: <code>nomor | judul | description | open/closed</code>. Field boleh dikosongkan.',
    'universalsearch':'Kirim kata pencarian. Bot mencari repo, code/file, issues/PR, commits, release, branch, dan tag.',
    'batchdesc':'Kirim description yang akan diterapkan ke seluruh repo batch. Kirim <code>-</code> untuk menghapus.',
    'newfolder':'Kirim nama/path folder relatif dari folder File Manager saat ini. Contoh: <code>assets/images</code>.',
    'createfilepath':'Kirim nama/path file relatif dari folder saat ini. Contoh: <code>config/settings.txt</code>. Setelah itu Anda bisa mengetik isi file atau mengirim file <code>.txt</code> sebagai sumber isi.'
}

async def ask(update,c,what):
    c.user_data['await']=what
    current=[]; full=repo_of(c)
    try:
        if full and what in ('desc','homeurl','defbranch','visibility'):
            r=await asyncio.to_thread(gh.repo,full)
            if what=='desc': current=['<b>Nilai saat ini</b>',f'<code>{esc(r.get("description") or "(kosong)")}</code>']
            elif what=='homeurl': current=['<b>Nilai saat ini</b>',f'<code>{esc(r.get("homepage") or "(kosong)")}</code>']
            elif what=='defbranch': current=['<b>Default branch saat ini</b>',f'<code>{esc(r.get("default_branch"))}</code>']
            elif what=='visibility': current=['<b>Visibility saat ini</b>',f'<code>{"private" if r.get("private") else "public"}</code>']
        elif full and what=='topics':
            names=await asyncio.to_thread(gh.topics,full); current=['<b>Topics saat ini</b>',(', '.join(f'<code>{esc(x)}</code>' for x in names) or '<code>(kosong)</code>')]
        elif full and what in ('release','releaseasset'):
            rs=await asyncio.to_thread(gh.releases,full); current=['<b>Release terbaru</b>']+[f'• <code>{esc(x.get("tag_name"))}</code> — {esc(x.get("name"))}' for x in rs[:8]]
        elif full and what in ('newissueedit','closeissue'):
            xs=await asyncio.to_thread(gh.issues,full); current=['<b>Open issues</b>']+[f'#{x["number"]} — {esc(x["title"])}' for x in xs[:12]]
        elif full and what in ('closepr','mergepr'):
            xs=await asyncio.to_thread(gh.pulls,full); current=['<b>Open pull requests</b>']+[f'#{x["number"]} — {esc(x["title"])} • <code>{esc(x["head"]["ref"])}→{esc(x["base"]["ref"])}</code>' for x in xs[:12]]
        elif full and what in ('secret','delsecret','var','delvar'):
            ss=await asyncio.to_thread(gh.secrets,full); vs=await asyncio.to_thread(gh.variables,full)
            if what in ('secret','delsecret'): current=['<b>Secrets saat ini</b>',(', '.join(f'<code>{esc(x["name"])}</code>' for x in ss) or '<code>(kosong)</code>')]
            else: current=['<b>Variables saat ini</b>']+[f'• <code>{esc(x["name"])}</code> = {esc(x.get("value"))}' for x in vs[:20]]
        elif full and what in ('newtag','deltag'):
            xs=await asyncio.to_thread(gh.tags,full); current=['<b>Tags saat ini</b>']+[f'• <code>{esc(x.get("name"))}</code>' for x in xs[:20]]
        elif what in ('commit','commitpush','showcommit','cherrypick','revert','compare','reset','newbranch','checkout') and full and (local_path(c)/'.git').exists():
            if what in ('newbranch','checkout','compare'): out=await asyncio.to_thread(safe_git,c,'branch','-a','--format=%(refname:short)')
            elif what in ('showcommit','cherrypick','revert','reset'): out=await asyncio.to_thread(safe_git,c,'log','-8','--oneline','--decorate')
            else: out=await asyncio.to_thread(safe_git,c,'status','--short','--branch')
            current=['<b>Data Git saat ini</b>',f'<pre>{esc(out)}</pre>']
        elif what=='renamefile' and c.user_data.get('fm_file'):
            p=Path(c.user_data['fm_file']); root=local_path(c).resolve(); current=['<b>File saat ini</b>',f'<code>{esc(p.relative_to(root))}</code>']
        elif what=='uploadpath' and full and (local_path(c)/'.git').exists():
            out=await asyncio.to_thread(safe_git,c,'status','--short','--branch'); current=['<b>Status repo lokal</b>',f'<pre>{esc(out)}</pre>']
    except Exception as e:
        current=['<i>Data saat ini tidak dapat dimuat: '+esc(e)+'</i>']
    body=("\n".join(current)+"\n\n" if current else '')+PROMPTS[what]+'\n\n/cancel untuk membatalkan.'
    await edit_or_send(update,body,kb([[btn('✖ Batal','cancel')]]))
async def cancel(update,c):
    c.user_data.pop('await',None); c.user_data.pop('await_document',None); c.user_data.pop('upload_target',None)
    await repo_panel(update,c)

async def text_input(update,c):
    if not admin(update): return await deny(update)
    uid=uid_of(update); activate_account(c,uid)
    text=(update.effective_message.text or '').rstrip('\n'); full=repo_of(c)
    if c.user_data.get('await_file_content'):
        try:
            target=Path(c.user_data.pop('create_file_target')).resolve(); c.user_data.pop('await_file_content',None)
            root=local_path(c).resolve()
            if target.exists(): await safety_backup(update,c,False)
            target.parent.mkdir(parents=True,exist_ok=True); target.write_text(text,encoding='utf-8')
            rel=target.relative_to(root); audit(uid,full,'create_file_text','ok',str(rel))
            return await update.effective_message.reply_text(f'✅ File dibuat dari teks Telegram.\n<code>{esc(rel)}</code>',parse_mode='HTML',reply_markup=kb([[btn('🧾 Diff','diffmenu'),btn('✅ Commit','ask:commit')],[btn('← File Manager','files')]]))
        except Exception as e: return await update.effective_message.reply_text(f'❌ {esc(e)}',parse_mode='HTML')
    mode=c.user_data.pop('await',None)
    if not mode: return
    text=text.strip()
    try:
        if mode=='newfolder':
            base=c.user_data.get('fm_dir','.'); root,folder=safe_repo_path(c,base); rel=text.replace('\\','/').strip().strip('/')
            if not rel or '..' in Path(rel).parts or '.git' in Path(rel).parts: raise ValueError('Path folder tidak valid')
            target=(folder/rel).resolve()
            if target!=root and not str(target).startswith(str(root)+os.sep): raise ValueError('Path keluar repo tidak diizinkan')
            target.mkdir(parents=True,exist_ok=True); keep=target/'.gitkeep'; keep.touch(exist_ok=True); audit(uid,full,'create_folder','ok',str(target.relative_to(root)))
            return await update.effective_message.reply_text(f'✅ Folder dibuat: <code>{esc(target.relative_to(root))}</code>\nFile <code>.gitkeep</code> ditambahkan agar folder dapat di-commit ke GitHub.',parse_mode='HTML',reply_markup=kb([[btn('🧾 Diff','diffmenu'),btn('✅ Commit','ask:commit')],[btn('📂 File Manager','files')]]))
        elif mode=='createfilepath':
            base=c.user_data.get('fm_dir','.'); root,folder=safe_repo_path(c,base); rel=text.replace('\\','/').strip().lstrip('/')
            if not rel or rel.endswith('/') or '..' in Path(rel).parts or '.git' in Path(rel).parts: raise ValueError('Path file tidak valid')
            target=(folder/rel).resolve()
            if not str(target).startswith(str(root)+os.sep): raise ValueError('Path keluar repo tidak diizinkan')
            c.user_data['create_file_target']=str(target); c.user_data['await_file_content']=True
            return await update.effective_message.reply_text(f'📝 Target: <code>{esc(target.relative_to(root))}</code>\n\nSekarang pilih salah satu:\n• ketik isi file langsung sebagai pesan teks, atau\n• kirim file <code>.txt</code> sebagai Document; isi .txt akan digunakan sebagai isi file target.',parse_mode='HTML',reply_markup=kb([[btn('✖ Batal','cancelupload')]]))
        elif mode=='desc': await asyncio.to_thread(gh.patch_repo,full,description=None if text=='-' else text)
        elif mode=='homeurl': await asyncio.to_thread(gh.patch_repo,full,homepage=None if text=='-' else text)
        elif mode=='topics':
            names=[re.sub(r'[^a-z0-9-]','-',x.strip().lower()).strip('-') for x in text.split(',')]
            await asyncio.to_thread(gh.set_topics,full,[x for x in names if x][:20])
        elif mode=='release':
            p=[x.strip() for x in text.split('|',2)]
            await asyncio.to_thread(gh.create_release,full,p[0],p[1] if len(p)>1 else p[0],p[2] if len(p)>2 else '')
        elif mode=='releaseasset':
            c.user_data['release_asset_tag']=text; c.user_data['await_release_asset']=True
            return await update.effective_message.reply_text(f'📎 Kirim file asset untuk release <code>{esc(text)}</code> sebagai Document.',parse_mode='HTML',reply_markup=kb([[btn('✖ Batal','cancelupload')]]))
        elif mode=='issue':
            p=[x.strip() for x in text.split('|',1)]
            await asyncio.to_thread(gh.create_issue,full,p[0],p[1] if len(p)>1 else '')
        elif mode=='newissueedit':
            p=[x.strip() for x in text.split('|',2)]
            if len(p)<2: raise ValueError('Format: nomor | judul baru | isi baru')
            num=int(p[0]); data={}
            if len(p)>1 and p[1]: data['title']=p[1]
            if len(p)>2 and p[2]: data['body']=p[2]
            if not data: raise ValueError('Tidak ada perubahan issue')
            await asyncio.to_thread(gh.patch_issue,full,num,**data)
        elif mode=='closeissue':
            num=int(text); c.user_data['pending_closeissue']=num
            return await update.effective_message.reply_text(f'Tutup issue <b>#{num}</b>?',parse_mode='HTML',reply_markup=kb([[btn('✅ Tutup','closeissueconfirm'),btn('✖ Batal','issues')]]))
        elif mode=='createpr':
            p=[x.strip() for x in text.split('|',3)]
            if len(p)<3: raise ValueError('Format: judul | head | base | isi')
            await asyncio.to_thread(gh.create_pull,full,p[0],p[1],p[2],p[3] if len(p)>3 else '')
        elif mode=='closepr':
            num=int(text); c.user_data['pending_closepr']=num
            return await update.effective_message.reply_text(f'Tutup PR <b>#{num}</b> tanpa merge?',parse_mode='HTML',reply_markup=kb([[btn('🚫 Tutup PR','closeprconfirm'),btn('✖ Batal','pulls')]]))
        elif mode=='mergepr':
            p=[x.strip().lower() for x in text.split('|',1)]; num=int(p[0]); method=p[1] if len(p)>1 and p[1] else 'squash'
            if method not in ('merge','squash','rebase'): raise ValueError('Metode harus merge, squash, atau rebase')
            c.user_data['pending_mergepr']=(num,method)
            return await update.effective_message.reply_text(f'Merge PR <b>#{num}</b> dengan metode <code>{method}</code>?',parse_mode='HTML',reply_markup=kb([[btn('✅ Merge','mergeprconfirm'),btn('✖ Batal','pulls')]]))
        elif mode=='secret':
            p=[x.strip() for x in text.split('|',1)]
            if len(p)<2: raise ValueError('Format secret harus NAMA | nilai')
            await asyncio.to_thread(gh.set_secret,full,p[0],p[1])
            try: await update.effective_message.delete()
            except Exception: pass
        elif mode=='delsecret': await asyncio.to_thread(gh.del_secret,full,text)
        elif mode=='var':
            p=[x.strip() for x in text.split('|',1)]
            if len(p)<2: raise ValueError('Format variable harus NAMA | nilai')
            await asyncio.to_thread(gh.set_variable,full,p[0],p[1])
        elif mode=='delvar': await asyncio.to_thread(gh.del_variable,full,text)
        elif mode=='defbranch': await asyncio.to_thread(gh.patch_repo,full,default_branch=text)
        elif mode=='visibility':
            if text not in ('public','private'): raise ValueError('Harus public atau private')
            c.user_data['pending_visibility']=text
            return await update.effective_message.reply_text(f'Konfirmasi ubah visibility menjadi <b>{text}</b>?',parse_mode='HTML',reply_markup=kb([[btn('✅ Ya','visconfirm'),btn('✖ Batal','repo')]]))
        elif mode=='commit':
            m=await update.effective_message.reply_text('⏳ <b>Commit</b>\n1/2 Stage semua perubahan...',parse_mode='HTML')
            await asyncio.to_thread(safe_git,c,'add','-A'); await m.edit_text('⏳ <b>Commit</b>\n✅ Stage\n2/2 Membuat commit...',parse_mode='HTML')
            out=await asyncio.to_thread(safe_git,c,'commit','-m',text); audit(uid_of(update),full,'commit','ok',out)
            return await m.edit_text('✅ <b>Commit berhasil</b>\n✅ Stage\n✅ Commit',parse_mode='HTML',reply_markup=kb([[btn('⬆️ Push sekarang','gitpush'),btn('← Git / Upload','localrepo')]]))
        elif mode=='commitpush':
            m=await update.effective_message.reply_text('⏳ <b>Commit + Push</b>\n1/3 Stage...',parse_mode='HTML')
            await asyncio.to_thread(safe_git,c,'add','-A'); await m.edit_text('⏳ <b>Commit + Push</b>\n✅ Stage\n2/3 Commit...',parse_mode='HTML')
            cout=await asyncio.to_thread(safe_git,c,'commit','-m',text); await m.edit_text('⏳ <b>Commit + Push</b>\n✅ Stage\n✅ Commit\n3/3 Push...',parse_mode='HTML')
            pout=await asyncio.to_thread(safe_git,c,'push'); audit(uid_of(update),full,'commit_push','ok',str(cout)+' | '+str(pout))
            return await m.edit_text('✅ <b>Commit + Push selesai</b>\n✅ Stage\n✅ Commit\n✅ Push',parse_mode='HTML',reply_markup=kb([[btn('← Git / Upload','localrepo')]]))
        elif mode=='newbranch':
            if not re.fullmatch(r'[A-Za-z0-9._/-]{1,120}',text) or text.startswith(('/', '-')) or '..' in text: raise ValueError('Nama branch tidak valid')
            await asyncio.to_thread(safe_git,c,'switch','-c',text)
            return await update.effective_message.reply_text(f'✅ Branch <code>{esc(text)}</code> dibuat dan aktif.',parse_mode='HTML',reply_markup=kb([[btn('← Git / Upload','localrepo')]]))
        elif mode=='checkout':
            if not re.fullmatch(r'[A-Za-z0-9._/-]{1,120}',text) or text.startswith(('/', '-')) or '..' in text: raise ValueError('Nama branch tidak valid')
            await asyncio.to_thread(safe_git,c,'switch',text)
            return await update.effective_message.reply_text(f'✅ Checkout ke <code>{esc(text)}</code>.',parse_mode='HTML',reply_markup=kb([[btn('← Git / Upload','localrepo')]]))
        elif mode=='showcommit':
            out=await asyncio.to_thread(safe_git,c,'show','--stat','--oneline','--decorate','--no-renames',text)
            return await update.effective_message.reply_text(f'<b>Commit Detail</b>\n<pre>{esc(out)}</pre>',parse_mode='HTML',reply_markup=kb([[btn('← Git / Upload','localrepo')]]))
        elif mode=='cherrypick':
            await asyncio.to_thread(safe_git,c,'cherry-pick',text)
            return await update.effective_message.reply_text('✅ Cherry-pick berhasil.',reply_markup=kb([[btn('← Git / Upload','localrepo')]]))
        elif mode=='revert':
            c.user_data['pending_revert']=text
            return await update.effective_message.reply_text(f'Buat revert commit untuk <code>{esc(text)}</code>?',parse_mode='HTML',reply_markup=kb([[btn('↩️ Revert','revertconfirm'),btn('✖ Batal','localrepo')]]))
        elif mode=='compare':
            p=[x.strip() for x in text.split('|',1)]
            if len(p)!=2: raise ValueError('Format: base | head')
            stat=await asyncio.to_thread(safe_git,c,'diff','--stat',f'{p[0]}...{p[1]}')
            logs=await asyncio.to_thread(safe_git,c,'log','--oneline',f'{p[0]}..{p[1]}')
            return await update.effective_message.reply_text(f'<b>Compare {esc(p[0])} ↔ {esc(p[1])}</b>\n\n<pre>{esc(stat)}\n\n{esc(logs)}</pre>',parse_mode='HTML',reply_markup=kb([[btn('← Git / Upload','localrepo')]]))
        elif mode=='newtag':
            p=[x.strip() for x in text.split('|',1)]; tag=p[0]
            if not re.fullmatch(r'[A-Za-z0-9._/-]{1,120}',tag): raise ValueError('Nama tag tidak valid')
            if len(p)>1 and p[1]: await asyncio.to_thread(safe_git,c,'tag','-a',tag,'-m',p[1])
            else: await asyncio.to_thread(safe_git,c,'tag',tag)
            await asyncio.to_thread(safe_git,c,'push','origin',tag)
            return await update.effective_message.reply_text(f'✅ Tag <code>{esc(tag)}</code> dibuat dan dipush.',parse_mode='HTML',reply_markup=kb([[btn('← Tags','tags')]]))
        elif mode=='deltag':
            c.user_data['pending_deltag']=text
            return await update.effective_message.reply_text(f'Hapus tag <code>{esc(text)}</code> lokal dan remote?',parse_mode='HTML',reply_markup=kb([[btn('🗑 Hapus Tag','deltagconfirm'),btn('✖ Batal','tags')]]))
        elif mode=='reset':
            p=[x.strip() for x in text.split('|',1)]
            if not p or not p[0]: raise ValueError('Ref wajib diisi')
            kind=(p[1].lower() if len(p)>1 and p[1] else 'mixed')
            if kind not in ('soft','mixed','hard'): raise ValueError('Mode harus soft/mixed/hard')
            c.user_data['pending_reset']=(p[0],kind)
            warning='⚠️ HARD reset dapat membuang perubahan working tree.' if kind=='hard' else 'Reset akan mengubah posisi/index repository.'
            return await update.effective_message.reply_text(f'{warning}\n\nRef: <code>{esc(p[0])}</code>\nMode: <b>{kind}</b>',parse_mode='HTML',reply_markup=kb([[btn('⚠️ Konfirmasi Reset','resetconfirm'),btn('✖ Batal','localrepo')]]))
        elif mode=='searchcode':
            xs=await asyncio.to_thread(gh.search_code,full,text)
            lines=[f'• <code>{esc(x.get("path"))}</code>' for x in xs[:20]]
            return await update.effective_message.reply_text('<b>Hasil Search Code</b>\n\n'+('\n'.join(lines) or 'Tidak ada hasil.'),parse_mode='HTML',reply_markup=kb([[btn('← Repo','repo')]]))
        elif mode=='newtext':
            p=text.split('|',1)
            if len(p)<2: raise ValueError('Format: path/file.txt | isi file')
            root,target=safe_repo_path(c,p[0].strip().lstrip('/'))
            if target==root or '.git' in target.relative_to(root).parts: raise ValueError('Path tidak valid')
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists(): await safety_backup(update,c,False)
            target.write_text(p[1].lstrip(),encoding='utf-8')
            return await update.effective_message.reply_text(f'✅ File dibuat: <code>{esc(target.relative_to(root))}</code>',parse_mode='HTML',reply_markup=kb([[btn('📂 File Manager','files'),btn('✅ Commit','ask:commit')]]))
        elif mode=='renamefile':
            src=Path(c.user_data.get('fm_file','')).resolve(); root=local_path(c).resolve()
            if not src.exists() or not str(src).startswith(str(root)+os.sep): raise ValueError('File sumber tidak valid')
            _,dst=safe_repo_path(c,text.strip().lstrip('/'))
            if dst==root or '.git' in dst.relative_to(root).parts: raise ValueError('Path tujuan tidak valid')
            dst.parent.mkdir(parents=True,exist_ok=True)
            if dst.exists(): await safety_backup(update,c,False)
            src.rename(dst); c.user_data['fm_file']=str(dst)
            return await update.effective_message.reply_text(f'✅ Rename: <code>{esc(dst.relative_to(root))}</code>',parse_mode='HTML',reply_markup=kb([[btn('📂 File Manager','files'),btn('✅ Commit','ask:commit')]]))
        elif mode=='createrepo':
            p=[x.strip() for x in text.split('|',2)]
            if not p or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',p[0]): raise ValueError('Nama repo tidak valid')
            vis=(p[1].lower() if len(p)>1 and p[1] else 'private')
            if vis not in ('public','private'): raise ValueError('Visibility harus public/private')
            r=await asyncio.to_thread(gh.create_repo,p[0],p[2] if len(p)>2 else '',vis=='private',True,active_owner())
            remember_repo(update,c,r['full_name']); audit(uid_of(update),r['full_name'],'create_repo')
            return await update.effective_message.reply_text(f'✅ Repository dibuat: <code>{esc(r["full_name"])}</code>',parse_mode='HTML',reply_markup=kb([[btn('📦 Buka Repo','repo')]]))
        elif mode=='renamerepo':
            if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',text): raise ValueError('Nama repo tidak valid')
            oldfull=full; r=await asyncio.to_thread(gh.patch_repo,full,name=text); remember_repo(update,c,r['full_name']); audit(uid_of(update),oldfull,'rename_repo','ok',r['full_name'])
            return await update.effective_message.reply_text(f'✅ Repo diubah menjadi <code>{esc(r["full_name"])}</code>',parse_mode='HTML',reply_markup=kb([[btn('← Repo','repo')]]))
        elif mode=='collabset':
            p=[x.strip() for x in text.split('|',1)]
            if len(p)!=2 or p[1] not in ('pull','triage','push','maintain','admin'): raise ValueError('Format/permission tidak valid')
            await asyncio.to_thread(gh.set_collaborator,full,p[0],p[1]); audit(uid_of(update),full,'set_collaborator','ok',f'{p[0]}:{p[1]}')
            return await update.effective_message.reply_text('✅ Collaborator/invite diproses.',reply_markup=kb([[btn('← Collaborators','collabs')]]))
        elif mode=='collabdel':
            c.user_data['pending_collabdel']=text
            return await update.effective_message.reply_text(f'Hapus collaborator <code>{esc(text)}</code>?',parse_mode='HTML',reply_markup=kb([[btn('🗑 Hapus','collabdelconfirm'),btn('✖ Batal','collabs')]]))
        elif mode=='rulesetdetail':
            x=await asyncio.to_thread(gh.rule,full,int(text)); payload=json.dumps(x,ensure_ascii=False,indent=2)[:3500]
            return await update.effective_message.reply_text(f'<b>Ruleset #{esc(text)}</b>\n<pre>{esc(payload)}</pre>',parse_mode='HTML',reply_markup=kb([[btn('← Rulesets','rulesets')]]))
        elif mode=='rulesetdel':
            c.user_data['pending_rulesetdel']=int(text)
            return await update.effective_message.reply_text(f'Hapus ruleset <code>#{esc(text)}</code>?',parse_mode='HTML',reply_markup=kb([[btn('🗑 Hapus Ruleset','rulesetdelconfirm'),btn('✖ Batal','rulesets')]]))
        elif mode=='labeladd':
            p=[x.strip() for x in text.split('|',2)]
            if len(p)<2 or not re.fullmatch(r'[0-9a-fA-F]{6}',p[1].lstrip('#')): raise ValueError('Format/color label tidak valid')
            await asyncio.to_thread(gh.create_label,full,p[0],p[1],p[2] if len(p)>2 else ''); audit(uid_of(update),full,'create_label','ok',p[0])
            return await update.effective_message.reply_text('✅ Label dibuat.',reply_markup=kb([[btn('← Labels','labels')]]))
        elif mode=='labeldel':
            c.user_data['pending_labeldel']=text
            return await update.effective_message.reply_text(f'Hapus label <code>{esc(text)}</code>?',parse_mode='HTML',reply_markup=kb([[btn('🗑 Hapus Label','labeldelconfirm'),btn('✖ Batal','labels')]]))
        elif mode=='milestoneadd':
            p=[x.strip() for x in text.split('|',1)]; await asyncio.to_thread(gh.create_milestone,full,p[0],p[1] if len(p)>1 else '')
            audit(uid_of(update),full,'create_milestone','ok',p[0]); return await update.effective_message.reply_text('✅ Milestone dibuat.',reply_markup=kb([[btn('← Labels/Milestones','labels')]]))
        elif mode=='milestoneclose':
            num=int(text); c.user_data['pending_milestoneclose']=num
            return await update.effective_message.reply_text(f'Tutup milestone <b>#{num}</b>?',parse_mode='HTML',reply_markup=kb([[btn('✅ Close','milestonecloseconfirm'),btn('✖ Batal','labels')]]))
        elif mode=='editrelease':
            rid=c.user_data.get('release_id'); old=await asyncio.to_thread(gh.release,full,rid); p=[x.strip() for x in text.split('|',3)]; data={}
            if len(p)>0 and p[0]: data['name']=p[0]
            if len(p)>1 and p[1]: data['body']=p[1]
            if len(p)>2 and p[2]: data['draft']=p[2].lower() in ('1','true','yes','ya')
            if len(p)>3 and p[3]: data['prerelease']=p[3].lower() in ('1','true','yes','ya')
            if not data: raise ValueError('Tidak ada perubahan')
            await asyncio.to_thread(gh.update_release,full,rid,**data); audit(uid_of(update),full,'edit_release','ok',str(rid))
            return await update.effective_message.reply_text('✅ Release diperbarui.',reply_markup=kb([[btn('← Releases','releases')]]))
        elif mode=='searchissues':
            xs=await asyncio.to_thread(gh.search_issues,full,text); lines=[f'• #{x.get("number")} {esc(x.get("title"))} [{esc(x.get("state"))}]' for x in xs[:30]]
            return await update.effective_message.reply_text('<b>Search Issues/PR</b>\n\n'+('\n'.join(lines) or 'Tidak ada hasil.'),parse_mode='HTML',reply_markup=kb([[btn('← Search','searchmenu')]]))
        elif mode=='searchcommits':
            xs=await asyncio.to_thread(gh.search_commits,full,text); lines=[f'• <code>{esc(x.get("sha","")[:8])}</code> {esc((x.get("commit") or {}).get("message","").splitlines()[0])}' for x in xs[:30]]
            return await update.effective_message.reply_text('<b>Search Commits</b>\n\n'+('\n'.join(lines) or 'Tidak ada hasil.'),parse_mode='HTML',reply_markup=kb([[btn('← Search','searchmenu')]]))
        elif mode=='batchtopic':
            topic=re.sub(r'[^a-z0-9-]','-',text.lower().strip()).strip('-')
            if not topic: raise ValueError('Topic tidak valid')
            selected=state_for(uid_of(update)).get('batch_repos',[]); ok=[]; fail=[]
            for rr in selected:
                try:
                    names=await asyncio.to_thread(gh.topics,rr)
                    if topic not in names: await asyncio.to_thread(gh.set_topics,rr,(names+[topic])[:20])
                    ok.append(rr)
                except Exception as e: fail.append(f'{rr}: {e}')
            audit(uid_of(update),'batch','batch_topic','ok',f'{topic}; ok={len(ok)} fail={len(fail)}')
            return await update.effective_message.reply_text(f'✅ Batch topic selesai. Berhasil: {len(ok)}, gagal: {len(fail)}',reply_markup=kb([[btn('← Batch','batch')]]))
        elif mode=='selectcommit':
            p=text.split('|',1)
            if len(p)!=2: raise ValueError('Format: file1,file2 | pesan commit')
            files=[x.strip() for x in p[0].split(',') if x.strip()]
            if not files: raise ValueError('Pilih minimal satu file')
            root=local_path(c).resolve()
            for rel in files:
                _,target=safe_repo_path(c,rel)
                if '.git' in target.relative_to(root).parts: raise ValueError('Path .git dilarang')
            await asyncio.to_thread(safe_git,c,'add','--',*files); await asyncio.to_thread(safe_git,c,'commit','-m',p[1].strip())
            audit(uid_of(update),full,'selective_commit','ok',','.join(files)); return await update.effective_message.reply_text('✅ Selective commit berhasil.',reply_markup=kb([[btn('⬆️ Push','gitpush'),btn('← Git','localrepo')]]))
        elif mode=='readmeedit':
            root=local_path(c); await safety_backup(update,c,False); (root/'README.md').write_text(text,encoding='utf-8')
            audit(uid_of(update),full,'readme_edit','ok'); return await update.effective_message.reply_text('✅ README.md diperbarui di working tree.',reply_markup=kb([[btn('🧾 Diff','gitdiff'),btn('✅ Commit','ask:commit')],[btn('← Git','localrepo')]]))
        elif mode=='template':
            made=await apply_template(update,c,text.lower().strip()); return await update.effective_message.reply_text('✅ Template diterapkan.\n'+('\n'.join('• '+esc(x) for x in made) if made else 'Tidak ada file baru karena semuanya sudah ada.'),parse_mode='HTML',reply_markup=kb([[btn('🧾 Diff','gitdiff'),btn('✅ Commit','ask:commit')],[btn('← Git','localrepo')]]))
        elif mode=='transferrepo':
            p=[x.strip() for x in text.split('|',1)]
            if not p or not p[0]: raise ValueError('Owner baru wajib diisi')
            c.user_data['pending_transfer']=(p[0],p[1] if len(p)>1 and p[1] else None)
            return await update.effective_message.reply_text(f'⚠️ Transfer <code>{esc(full)}</code> ke owner <code>{esc(p[0])}</code>'+(f' dengan nama <code>{esc(p[1])}</code>' if len(p)>1 and p[1] else '')+'?',parse_mode='HTML',reply_markup=kb([[btn('🔁 Konfirmasi Transfer','transferconfirm'),btn('✖ Batal','repoadmin')]]))
        elif mode=='protectbranch':
            p=[x.strip() for x in text.split('|',1)]; branch=p[0]; reviews=int(p[1]) if len(p)>1 and p[1] else 1
            c.user_data['pending_protect']=(branch,reviews)
            return await update.effective_message.reply_text(f'Protect branch <code>{esc(branch)}</code> dengan minimal <b>{reviews}</b> approval?',parse_mode='HTML',reply_markup=kb([[btn('🛡 Protect','protectconfirm'),btn('✖ Batal','rulesets')]]))
        elif mode=='unprotectbranch':
            c.user_data['pending_unprotect']=text
            return await update.effective_message.reply_text(f'Hapus protection branch <code>{esc(text)}</code>?',parse_mode='HTML',reply_markup=kb([[btn('🔓 Unprotect','unprotectconfirm'),btn('✖ Batal','rulesets')]]))
        elif mode=='labeledit':
            p=[x.strip() for x in text.split('|',3)]
            if not p or not p[0]: raise ValueError('Nama label lama wajib diisi')
            data={}
            if len(p)>1 and p[1]: data['new_name']=p[1]
            if len(p)>2 and p[2]:
                if not re.fullmatch(r'[0-9a-fA-F]{6}',p[2].lstrip('#')): raise ValueError('Color harus 6 digit hex')
                data['color']=p[2]
            if len(p)>3 and p[3]: data['description']=p[3]
            if not data: raise ValueError('Tidak ada perubahan')
            await asyncio.to_thread(gh.update_label,full,p[0],**data); audit(uid_of(update),full,'edit_label','ok',p[0]); return await update.effective_message.reply_text('✅ Label diperbarui.',reply_markup=kb([[btn('← Labels','labels')]]))
        elif mode=='labelassign':
            p=[x.strip() for x in text.split('|',1)]
            if len(p)!=2: raise ValueError('Format: nomor | label1,label2')
            num=int(p[0]); labels=[x.strip() for x in p[1].split(',') if x.strip()]
            await asyncio.to_thread(gh.set_issue_labels,full,num,labels); audit(uid_of(update),full,'assign_labels','ok',f'{num}:{labels}'); return await update.effective_message.reply_text('✅ Label diterapkan.',reply_markup=kb([[btn('← Labels','labels')]]))
        elif mode=='milestoneedit':
            p=[x.strip() for x in text.split('|',3)]; num=int(p[0]); data={}
            if len(p)>1 and p[1]: data['title']=p[1]
            if len(p)>2 and p[2]: data['description']=p[2]
            if len(p)>3 and p[3]:
                if p[3] not in ('open','closed'): raise ValueError('State harus open/closed')
                data['state']=p[3]
            if not data: raise ValueError('Tidak ada perubahan')
            await asyncio.to_thread(gh.update_milestone,full,num,**data); audit(uid_of(update),full,'edit_milestone','ok',num); return await update.effective_message.reply_text('✅ Milestone diperbarui.',reply_markup=kb([[btn('← Labels/Milestones','labels')]]))
        elif mode=='universalsearch':
            q=text; blocks=[]
            try:
                rr=await asyncio.to_thread(gh.search_repositories,q); blocks.append('<b>Repositories</b>\n'+('\n'.join(f'• <code>{esc(x.get("full_name"))}</code>' for x in rr[:6]) or '—'))
            except Exception: pass
            if full:
                try:
                    cc=await asyncio.to_thread(gh.search_code,full,q); blocks.append('<b>Code/Files</b>\n'+('\n'.join(f'• <code>{esc(x.get("path"))}</code>' for x in cc[:8]) or '—'))
                except Exception: pass
                try:
                    ii=await asyncio.to_thread(gh.search_issues,full,q); blocks.append('<b>Issues/PR</b>\n'+('\n'.join(f'• #{x.get("number")} {esc(x.get("title"))}' for x in ii[:8]) or '—'))
                except Exception: pass
                try:
                    co=await asyncio.to_thread(gh.search_commits,full,q); blocks.append('<b>Commits</b>\n'+('\n'.join(f'• <code>{esc(x.get("sha","")[:8])}</code> {esc((x.get("commit") or {}).get("message","").splitlines()[0])}' for x in co[:8]) or '—'))
                except Exception: pass
                try:
                    rels=await asyncio.to_thread(gh.releases,full); m=[x for x in rels if q.lower() in ((x.get('tag_name') or '')+' '+(x.get('name') or '')).lower()]; blocks.append('<b>Releases</b>\n'+('\n'.join(f'• <code>{esc(x.get("tag_name"))}</code> {esc(x.get("name"))}' for x in m[:8]) or '—'))
                except Exception: pass
                try:
                    br=await asyncio.to_thread(gh.branches,full); tg=await asyncio.to_thread(gh.tags,full)
                    bm=[x.get('name') for x in br if q.lower() in x.get('name','').lower()]; tm=[x.get('name') for x in tg if q.lower() in x.get('name','').lower()]
                    blocks.append('<b>Branches/Tags</b>\n'+('\n'.join('• <code>'+esc(x)+'</code>' for x in (bm+tm)[:12]) or '—'))
                except Exception: pass
            return await update.effective_message.reply_text('<b>Unified Search</b>\n\n'+'\n\n'.join(blocks),parse_mode='HTML',reply_markup=kb([[btn('← Search','searchmenu')]]))
        elif mode=='batchdesc':
            selected=state_for(uid_of(update)).get('batch_repos',[]); ok=0; fail=[]
            for rr in selected:
                try: await asyncio.to_thread(gh.patch_repo,rr,description=None if text=='-' else text); ok+=1
                except Exception as e: fail.append(rr)
            cache_clear(); audit(uid_of(update),'batch','batch_description','ok',f'ok={ok},fail={len(fail)}'); return await update.effective_message.reply_text(f'✅ Batch description selesai. Berhasil: {ok}, gagal: {len(fail)}',reply_markup=kb([[btn('← Batch','batch')]]))
        elif mode=='diffpath':
            rel=text.strip(); _,target=safe_repo_path(c,rel); root=local_path(c).resolve()
            if target==root or '.git' in target.relative_to(root).parts: raise ValueError('Path tidak valid')
            out=await asyncio.to_thread(safe_git,c,'diff','--','%s'%rel)
            staged=await asyncio.to_thread(safe_git,c,'diff','--cached','--','%s'%rel)
            return await update.effective_message.reply_text(f'<b>Diff {esc(rel)}</b>\n\n<b>Unstaged</b>\n<pre>{esc(out)}</pre>\n\n<b>Staged</b>\n<pre>{esc(staged)}</pre>',parse_mode='HTML',reply_markup=kb([[btn('← Diff','diffmenu')]]))
        elif mode=='discardfile':
            rel=text.strip(); _,target=safe_repo_path(c,rel); root=local_path(c).resolve()
            if target==root or '.git' in target.relative_to(root).parts: raise ValueError('Path tidak valid')
            c.user_data['pending_discard']=rel
            return await update.effective_message.reply_text(f'⚠️ Buang perubahan lokal pada <code>{esc(rel)}</code>? Safety branch akan dibuat lebih dulu.',parse_mode='HTML',reply_markup=kb([[btn('🛟 Backup + Discard','discardconfirm'),btn('✖ Batal','diffmenu')]]))
        elif mode=='uploadpath':
            p=(text or '').replace('\\','/').strip().lstrip('/')
            if not p or p.endswith('/') or '..' in Path(p).parts or p.startswith('.git/') or p=='.git': raise ValueError('Path tujuan tidak valid')
            root=local_path(c).resolve()
            if not (root/'.git').exists(): raise ValueError('Repository belum di-clone')
            target=(root/p).resolve()
            if not str(target).startswith(str(root)+os.sep): raise ValueError('Path keluar dari repository tidak diizinkan')
            c.user_data['upload_target']=str(target); c.user_data['await_document']=True
            return await update.effective_message.reply_text(f'📤 Sekarang kirim file sebagai <b>Document</b>.\n\nTujuan: <code>{esc(p)}</code>',parse_mode='HTML',reply_markup=kb([[btn('✖ Batal','cancelupload')]]))
        cache_clear(); audit(uid_of(update),full,mode,'ok')
        await update.effective_message.reply_text('✅ Berhasil diproses.',reply_markup=kb([[btn('← Kembali ke Repo','repo')]]))
    except Exception as e:
        c.user_data['await']=mode; audit(uid_of(update),full,mode,'error',e)
        await update.effective_message.reply_text(f'❌ {esc(e)}\n\nCoba lagi atau /cancel.',parse_mode='HTML')

async def document_input(update,c):
    if not admin(update): return await deny(update)
    uid=uid_of(update); activate_account(c,uid); doc=update.effective_message.document
    try:
        if c.user_data.get('await_file_content'):
            if not (doc.file_name or '').lower().endswith('.txt'): raise ValueError('Untuk sumber isi file, kirim Document .txt')
            if (doc.file_size or 0)>2*1024*1024: raise ValueError('File .txt sumber maksimal 2 MB')
            target=Path(c.user_data.pop('create_file_target')).resolve(); c.user_data.pop('await_file_content',None)
            root=local_path(c).resolve(); tmp=BASE/'tmp'; tmp.mkdir(exist_ok=True); src=tmp/f'content-{int(time.time())}.txt'
            tgfile=await doc.get_file(); await tgfile.download_to_drive(custom_path=str(src))
            try: content=src.read_text(encoding='utf-8-sig',errors='replace')
            finally:
                try: src.unlink()
                except Exception: pass
            if target.exists(): await safety_backup(update,c,False)
            target.parent.mkdir(parents=True,exist_ok=True); target.write_text(content,encoding='utf-8')
            rel=target.relative_to(root); audit(uid,repo_of(c),'create_file_from_txt','ok',str(rel))
            return await update.effective_message.reply_text(f'✅ File dibuat dari isi <code>{esc(doc.file_name)}</code>.\nTarget: <code>{esc(rel)}</code>',parse_mode='HTML',reply_markup=kb([[btn('🧾 Diff','diffmenu'),btn('✅ Commit','ask:commit')],[btn('← File Manager','files')]]))
        if c.user_data.pop('await_release_asset',False):
            tag=c.user_data.pop('release_asset_tag',None)
            if not tag: raise ValueError('Tag release tidak ditemukan')
            tmp=BASE/'tmp'; tmp.mkdir(exist_ok=True); target=tmp/(Path(doc.file_name or 'asset.bin').name)
            tgfile=await doc.get_file(); await tgfile.download_to_drive(custom_path=str(target))
            try: asset=await asyncio.to_thread(gh.upload_release_asset,repo_of(c),tag,target)
            finally:
                try: target.unlink()
                except Exception: pass
            audit(uid_of(update),repo_of(c),'upload_release_asset','ok',asset.get('name'))
            return await update.effective_message.reply_text(f'✅ Release asset diupload.\n<code>{esc(asset.get("name"))}</code>',parse_mode='HTML',reply_markup=kb([[btn('← Releases','releases')]]))
        if c.user_data.get('await_multi'):
            root,folder=safe_repo_path(c,c.user_data.get('fm_dir','.')); name=Path(doc.file_name or 'upload.bin').name; target=(folder/name).resolve()
            if not str(target).startswith(str(root)+os.sep): raise ValueError('Nama/path file tidak aman')
            if target.exists() and not c.user_data.get('multi_backup_done'):
                await safety_backup(update,c,False); c.user_data['multi_backup_done']=True
            tgfile=await doc.get_file(); await tgfile.download_to_drive(custom_path=str(target)); audit(uid_of(update),repo_of(c),'multi_upload','ok',str(target.relative_to(root)))
            return await update.effective_message.reply_text(f'✅ <code>{esc(name)}</code> tersimpan di <code>{esc(folder.relative_to(root))}</code>.\nKirim file berikutnya atau tekan Selesai.',parse_mode='HTML',reply_markup=kb([[btn('✅ Selesai Multi Upload','fmmultidone')]]))
        if c.user_data.pop('await_zip',False):
            root,folder=safe_repo_path(c,c.user_data.get('fm_dir','.'))
            if not (doc.file_name or '').lower().endswith('.zip'): raise ValueError('File harus ZIP')
            tmp=BASE/'tmp'; tmp.mkdir(exist_ok=True); zpath=tmp/Path(doc.file_name).name
            tgfile=await doc.get_file(); await tgfile.download_to_drive(custom_path=str(zpath))
            try:
                with zipfile.ZipFile(zpath) as z:
                    members=z.infolist(); total=sum(x.file_size for x in members)
                    if total>250*1024*1024: raise ValueError('Isi ZIP terlalu besar (>250MB)')
                    overwrite=False
                    for m in members:
                        dest=(folder/m.filename).resolve()
                        if dest!=folder and not str(dest).startswith(str(folder)+os.sep): raise ValueError('ZIP mengandung path tidak aman')
                        if dest.exists(): overwrite=True
                    if overwrite: await safety_backup(update,c,False)
                    z.extractall(folder)
            finally:
                try: zpath.unlink()
                except Exception: pass
            audit(uid_of(update),repo_of(c),'extract_zip','ok',str(folder.relative_to(root)))
            return await update.effective_message.reply_text(f'✅ ZIP diekstrak ke <code>{esc(folder.relative_to(root))}</code>.',parse_mode='HTML',reply_markup=kb([[btn('📂 File Manager','files'),btn('✅ Commit','ask:commit')]]))
        if not c.user_data.get('await_document'):
            return await update.effective_message.reply_text('Pilih menu upload terlebih dahulu.',reply_markup=kb([[btn('🏠 Menu','home')]]))
        target_s=c.user_data.pop('upload_target',None); c.user_data.pop('await_document',None)
        if not target_s: raise ValueError('Target upload tidak ditemukan')
        target=Path(target_s).resolve(); root=local_path(c).resolve()
        if not str(target).startswith(str(root)+os.sep): raise ValueError('Path upload tidak aman')
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists(): await safety_backup(update,c,False)
        tgfile=await doc.get_file(); await tgfile.download_to_drive(custom_path=str(target))
        rel=target.relative_to(root); status=await asyncio.to_thread(safe_git,c,'status','--short','--',str(rel)); audit(uid_of(update),repo_of(c),'upload_file','ok',str(rel))
        await update.effective_message.reply_text(f'✅ File tersimpan.\n\nRepo: <code>{esc(repo_of(c))}</code>\nPath: <code>{esc(rel)}</code>\nNama upload: <code>{esc(doc.file_name)}</code>\n\n<pre>{esc(status)}</pre>',parse_mode='HTML',reply_markup=kb([[btn('🧾 Lihat Diff','gitdiff'),btn('✅ Commit','ask:commit')],[btn('✅ Commit + Push','ask:commitpush'),btn('← Git / Upload','localrepo')]]))
    except Exception as e:
        await update.effective_message.reply_text(f'❌ Upload gagal: <code>{esc(e)}</code>',parse_mode='HTML',reply_markup=kb([[btn('← Git / Upload','localrepo')]]))

async def media_input(update,c):
    if not admin(update): return await deny(update)
    uid=uid_of(update); activate_account(c,uid)
    if not c.user_data.get('await_media'):
        return await update.effective_message.reply_text('Pilih menu Upload Media dari File Manager terlebih dahulu.')
    m=update.effective_message; obj=None; default_ext='.bin'; name=None
    if m.photo: obj=m.photo[-1]; default_ext='.jpg'
    elif m.video: obj=m.video; default_ext='.mp4'; name=getattr(obj,'file_name',None)
    elif m.audio: obj=m.audio; default_ext='.mp3'; name=getattr(obj,'file_name',None)
    elif m.voice: obj=m.voice; default_ext='.ogg'
    elif m.animation: obj=m.animation; default_ext='.gif'; name=getattr(obj,'file_name',None)
    elif m.video_note: obj=m.video_note; default_ext='.mp4'
    if not obj: return
    try:
        root,folder=safe_repo_path(c,c.user_data.get('fm_dir','.'))
        stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        safe_name=Path(name).name if name else f'media-{stamp}{default_ext}'
        safe_name=re.sub(r'[^A-Za-z0-9._ -]','_',safe_name)[:180]
        target=(folder/safe_name).resolve()
        if not str(target).startswith(str(root)+os.sep): raise ValueError('Nama media tidak aman')
        if target.exists(): target=folder/f'{Path(safe_name).stem}-{stamp}{Path(safe_name).suffix or default_ext}'
        tgfile=await obj.get_file(); await tgfile.download_to_drive(custom_path=str(target))
        rel=target.relative_to(root); audit(uid,repo_of(c),'upload_media','ok',str(rel))
        await update.effective_message.reply_text(f'✅ Media tersimpan.\n<code>{esc(rel)}</code>\n\nKirim media berikutnya atau tekan Selesai.',parse_mode='HTML',reply_markup=kb([[btn('✅ Selesai Upload Media','fmmediadone')],[btn('🧾 Diff','diffmenu')]]))
    except Exception as e:
        await update.effective_message.reply_text(f'❌ Upload media gagal: <code>{esc(e)}</code>',parse_mode='HTML')

async def botstatus(update,c):
    k=activate_account(c,uid_of(update)); a=GITHUB_ACCOUNTS[k]
    t=(f'<b>Status Bot</b>\n\nTelegram token: {"✅" if BOT_TOKEN else "❌"}\n'
       f'Akun aktif: <b>{esc(a.get("alias"))}</b>\nOwner aktif: <code>{esc(a.get("owner") or "—")}</code>\n'
       f'GitHub akun 1: {"✅" if GH_TOKEN else "❌"}\nGitHub akun 2: {"✅" if GH_TOKEN_2 else "⚪ belum diisi"}\n'
       f'Admin restriction: {"✅" if ADMINS else "❌"}\nLocal base: <code>{esc(LOCAL_BASE)}</code>')
    await edit_or_send(update,t,kb([[btn('🔁 Ganti Akun','accountswitch')],[btn('← Home','home')]]))

async def show_error(update,e,back='repo'):
    log.error('Operation failed: %s',e); await edit_or_send(update,f'❌ <b>Operasi gagal</b>\n<code>{esc(str(e))}</code>',kb([[btn('← Kembali',back)]]))

async def callbacks(update:Update,c:ContextTypes.DEFAULT_TYPE):
    if not admin(update): return await deny(update)
    q=update.callback_query; await q.answer(); d=q.data; uid=uid_of(update); activate_account(c,uid)
    if not c.user_data.get('repo'):
        persisted=state_for(uid).get('repo')
        if persisted: c.user_data['repo']=persisted
    try:
        if d=='noop': return
        # Main navigation
        if d=='home': return await home(update,c)
        if d=='account': return await account(update,c)
        if d=='accountswitch': return await account_switch_panel(update,c)
        if d.startswith('accountset:'): return await switch_account(update,c,d.split(':',1)[1])
        if d=='favorites': return await favorites(update,c)
        if d=='recent': return await recent(update,c)
        if d=='tokenhealth': return await token_health(update,c)
        if d=='audit': return await audit_panel(update,c)
        if d=='activity': return await activity_panel(update,c)
        if d=='health': return await health(update,c)
        if d=='security': return await security_panel(update,c)
        if d=='searchmenu': return await search_menu(update,c)
        if d=='batch': return await batch_panel(update,c)
        if d=='notifycfg': return await notifications_panel(update,c)
        if d=='repoadmin': return await repo_admin(update,c)
        if d=='collabs': return await collaborators_panel(update,c)
        if d.startswith('collabpage:'): return await collaborators_panel(update,c,int(d.split(':')[1]))
        if d=='rulesets': return await rulesets_panel(update,c)
        if d=='labels': return await labels_panel(update,c)
        if d.startswith('labelpage:'): return await labels_panel(update,c,int(d.split(':')[1]))
        if d=='workitems': return await workitems(update,c)
        if d in ('local','localrepo'): return await local_panel(update,c)
        if d=='botstatus': return await botstatus(update,c)
        # Repository selection / persistence
        if d.startswith('repos:'): return await repos(update,c,int(d.split(':')[1]))
        if d=='reposrefresh': cache_clear('repos'); return await repos(update,c,0)
        if d.startswith('rsel:'):
            full=c.user_data['repos'][int(d.split(':')[1])]; remember_repo(update,c,full); return await repo_panel(update,c)
        if d.startswith('qrepo:'):
            full=c.user_data['quick_repos'][int(d.split(':')[1])]; remember_repo(update,c,full); return await repo_panel(update,c)
        if d=='repo': return await repo_panel(update,c)
        if d=='favtoggle':
            on=favorite_toggle(uid,repo_of(c)); audit(uid,repo_of(c),'favorite_toggle','ok',str(on)); return await repo_admin(update,c)
        # Metadata / settings
        if d=='meta': return await metadata(update,c)
        if d=='topics': return await topics(update,c)
        if d=='branches': return await branches(update,c)
        if d.startswith('branchpage:'): return await branches(update,c,int(d.split(':')[1]))
        if d=='tags': return await tags_panel(update,c)
        if d.startswith('tagpage:'): return await tags_panel(update,c,int(d.split(':')[1]))
        if d=='secvars': return await secvars(update,c)
        if d=='settings': return await settings(update,c)
        if d.startswith('ask:'):
            what=d.split(':',1)[1]
            if what in ('newfolder','createfilepath','uploadpath'):
                await ensure_local_repo(update,c,False); c.user_data.setdefault('fm_dir','.')
            return await ask(update,c,what)
        if d=='cancel': return await cancel(update,c)
        if d.startswith('tog:'):
            key=d.split(':',1)[1]; r=await asyncio.to_thread(gh.repo,repo_of(c)); await asyncio.to_thread(gh.patch_repo,repo_of(c),**{key:not bool(r.get(key))}); cache_clear(); audit(uid,repo_of(c),'toggle_setting','ok',key); return await settings(update,c)
        if d=='archive1': return await edit_or_send(update,'⚠️ <b>Archive repository?</b>\nRepository menjadi read-only sampai di-unarchive.',kb([[btn('⚠️ KONFIRMASI ARCHIVE','archive2'),btn('✖ Batal','repoadmin')]]))
        if d=='archive2': await asyncio.to_thread(gh.patch_repo,repo_of(c),archived=True); audit(uid,repo_of(c),'archive_repo'); return await repo_admin(update,c)
        if d=='unarchive': await asyncio.to_thread(gh.patch_repo,repo_of(c),archived=False); audit(uid,repo_of(c),'unarchive_repo'); return await repo_admin(update,c)
        if d=='visconfirm':
            vis=c.user_data.pop('pending_visibility',None)
            if vis: await asyncio.to_thread(gh.patch_repo,repo_of(c),visibility=vis); audit(uid,repo_of(c),'visibility','ok',vis)
            return await repo_panel(update,c)
        if d=='deleterepo1':
            return await edit_or_send(update,f'⚠️ <b>DELETE REPOSITORY?</b>\n\nRepo: <code>{esc(repo_of(c))}</code>\nIni menghapus repository dari GitHub. Konfirmasi tahap 1/2.',kb([[btn('⚠️ Lanjut Konfirmasi','deleterepo2'),btn('✖ Batal','repoadmin')]]))
        if d=='deleterepo2':
            c.user_data['delete_repo_full']=repo_of(c)
            return await edit_or_send(update,f'🚨 <b>KONFIRMASI TERAKHIR</b>\nHapus permanen <code>{esc(repo_of(c))}</code>?',kb([[btn('🗑 HAPUS PERMANEN','deleterepo3'),btn('✖ Batal','repoadmin')]]))
        if d=='deleterepo3':
            full=c.user_data.pop('delete_repo_full',None)
            if not full: raise ValueError('Konfirmasi delete kedaluwarsa')
            await asyncio.to_thread(gh.delete_repo,full); audit(uid,full,'delete_repo'); c.user_data.pop('repo',None); state_patch(uid,repo=None); cache_clear()
            return await edit_or_send(update,'✅ Repository dihapus dari GitHub.',kb([[btn('🏠 Home','home')]]))
        if d=='transferconfirm':
            pending=c.user_data.pop('pending_transfer',None)
            if not pending: raise ValueError('Data transfer kedaluwarsa')
            owner,newname=pending; old=repo_of(c); r=await asyncio.to_thread(gh.transfer_repo,old,owner,newname); audit(uid,old,'transfer_repo','ok',f'{owner}:{newname or ""}')
            # Transfer GitHub dapat bersifat asynchronous; kembali ke daftar repo agar nama/owner terbaru dimuat ulang.
            cache_clear(); c.user_data.pop('repo',None); state_patch(uid,repo=None)
            return await edit_or_send(update,'✅ Permintaan transfer repository dikirim ke GitHub.',kb([[btn('📦 Muat Ulang Repo','repos:0')]]))
        # Collaborators / rulesets / labels / milestones
        if d=='collabdelconfirm':
            user=c.user_data.pop('pending_collabdel',None)
            if not user: raise ValueError('Data collaborator kedaluwarsa')
            await asyncio.to_thread(gh.del_collaborator,repo_of(c),user); audit(uid,repo_of(c),'delete_collaborator','ok',user); return await collaborators_panel(update,c)
        if d=='rulesetdelconfirm':
            rid=c.user_data.pop('pending_rulesetdel',None)
            if not rid: raise ValueError('Data ruleset kedaluwarsa')
            await asyncio.to_thread(gh.delete_ruleset,repo_of(c),rid); audit(uid,repo_of(c),'delete_ruleset','ok',rid); return await rulesets_panel(update,c)
        if d=='protectconfirm':
            pending=c.user_data.pop('pending_protect',None)
            if not pending: raise ValueError('Data protection kedaluwarsa')
            branch,reviews=pending; await asyncio.to_thread(gh.protect_branch,repo_of(c),branch,reviews); audit(uid,repo_of(c),'protect_branch','ok',f'{branch}:{reviews}'); return await rulesets_panel(update,c)
        if d=='unprotectconfirm':
            branch=c.user_data.pop('pending_unprotect',None)
            if not branch: raise ValueError('Data protection kedaluwarsa')
            await asyncio.to_thread(gh.unprotect_branch,repo_of(c),branch); audit(uid,repo_of(c),'unprotect_branch','ok',branch); return await rulesets_panel(update,c)
        if d=='labeldelconfirm':
            name=c.user_data.pop('pending_labeldel',None)
            if not name: raise ValueError('Data label kedaluwarsa')
            await asyncio.to_thread(gh.delete_label,repo_of(c),name); audit(uid,repo_of(c),'delete_label','ok',name); return await labels_panel(update,c)
        if d=='milestonecloseconfirm':
            num=c.user_data.pop('pending_milestoneclose',None)
            if not num: raise ValueError('Data milestone kedaluwarsa')
            await asyncio.to_thread(gh.close_milestone,repo_of(c),num); audit(uid,repo_of(c),'close_milestone','ok',num); return await labels_panel(update,c)
        # Work items pagination/details
        if d=='issues': return await issues(update,c,0)
        if d.startswith('issuepage:'): return await issues(update,c,int(d.split(':')[1]))
        if d.startswith('issuedetail:'): return await issue_detail(update,c,int(d.split(':')[1]))
        if d=='issueeditselected':
            num=c.user_data.get('issue_num')
            if not num: raise ValueError('Issue tidak dipilih')
            c.user_data['await']='newissueedit'; return await edit_or_send(update,f'Issue aktif: <b>#{num}</b>\nFormat: <code>{num} | judul baru | isi baru</code>',kb([[btn('✖ Batal','issues')]]))
        if d=='issuecloseselected':
            num=c.user_data.get('issue_num'); c.user_data['pending_closeissue']=num
            return await edit_or_send(update,f'Tutup issue <b>#{num}</b>?',kb([[btn('✅ Tutup','closeissueconfirm'),btn('✖ Batal','issues')]]))
        if d=='closeissueconfirm':
            num=c.user_data.pop('pending_closeissue',None)
            if not num: raise ValueError('Data issue kedaluwarsa')
            await asyncio.to_thread(gh.patch_issue,repo_of(c),num,state='closed'); audit(uid,repo_of(c),'close_issue','ok',num); return await issues(update,c)
        if d=='pulls': return await pulls(update,c,0)
        if d.startswith('prpage:'): return await pulls(update,c,int(d.split(':')[1]))
        if d.startswith('prdetail:'): return await pull_detail(update,c,int(d.split(':')[1]))
        if d.startswith('prmerge:'):
            method=d.split(':')[1]; num=c.user_data.get('pr_num'); c.user_data['pending_mergepr']=(num,method)
            return await edit_or_send(update,f'Merge PR <b>#{num}</b> dengan <code>{method}</code>?',kb([[btn('✅ Merge','mergeprconfirm'),btn('✖ Batal','pulls')]]))
        if d=='prcloseselected':
            num=c.user_data.get('pr_num'); c.user_data['pending_closepr']=num
            return await edit_or_send(update,f'Tutup PR <b>#{num}</b> tanpa merge?',kb([[btn('🚫 Tutup PR','closeprconfirm'),btn('✖ Batal','pulls')]]))
        if d=='closeprconfirm':
            num=c.user_data.pop('pending_closepr',None)
            if not num: raise ValueError('Data PR kedaluwarsa')
            await asyncio.to_thread(gh.patch_pull,repo_of(c),num,state='closed'); audit(uid,repo_of(c),'close_pr','ok',num); return await pulls(update,c)
        if d=='mergeprconfirm':
            pending=c.user_data.pop('pending_mergepr',None)
            if not pending: raise ValueError('Data merge PR kedaluwarsa')
            num,method=pending; result=await asyncio.to_thread(gh.merge_pull,repo_of(c),num,method)
            if not result.get('merged'): raise ValueError(result.get('message','PR gagal di-merge'))
            audit(uid,repo_of(c),'merge_pr','ok',f'{num}:{method}'); return await pulls(update,c)
        # Releases
        if d=='releases': return await releases(update,c,0)
        if d.startswith('relpage:'): return await releases(update,c,int(d.split(':')[1]))
        if d.startswith('reldetail:'): return await release_detail(update,c,int(d.split(':')[1]))
        if d=='releaseassetselected':
            tag=c.user_data.get('release_tag')
            if not tag: raise ValueError('Release tidak dipilih')
            c.user_data['release_asset_tag']=tag; c.user_data['await_release_asset']=True
            return await edit_or_send(update,f'📎 Kirim asset untuk release <code>{esc(tag)}</code> sebagai Document.',kb([[btn('✖ Batal','releases')]]))
        if d=='delrelease1': return await edit_or_send(update,'⚠️ Hapus release ini? Tag Git tidak otomatis dihapus.',kb([[btn('🗑 Hapus Release','delrelease2'),btn('✖ Batal','releases')]]))
        if d=='delrelease2':
            rid=c.user_data.get('release_id'); await asyncio.to_thread(gh.delete_release,repo_of(c),rid); audit(uid,repo_of(c),'delete_release','ok',rid); return await releases(update,c)
        if d.startswith('delasset:'):
            aid=int(d.split(':')[1]); c.user_data['pending_assetdel']=aid
            return await edit_or_send(update,f'Hapus release asset <code>#{aid}</code>?',kb([[btn('🗑 Hapus Asset','delassetconfirm'),btn('✖ Batal','releases')]]))
        if d=='delassetconfirm':
            aid=c.user_data.pop('pending_assetdel',None)
            if not aid: raise ValueError('Asset kedaluwarsa')
            await asyncio.to_thread(gh.delete_asset,repo_of(c),aid); audit(uid,repo_of(c),'delete_release_asset','ok',aid); return await releases(update,c)
        # Actions runs / workflows
        if d=='actions': return await actions(update,c,0)
        if d.startswith('actionpage:'): return await actions(update,c,int(d.split(':')[1]))
        if d.startswith('rundetail:'): return await run_detail(update,c,int(d.split(':')[1]))
        if d=='runlogs':
            rid=c.user_data.get('run_id')
            if not rid: raise ValueError('Workflow run belum dipilih')
            tmp=BASE/'tmp'; tmp.mkdir(exist_ok=True); path=tmp/f'workflow-{rid}-logs.zip'
            try:
                await asyncio.to_thread(gh.download_run_logs,repo_of(c),rid,path)
                await q.message.reply_document(document=path.open('rb'),filename=path.name,caption=f'Workflow logs #{rid}')
            finally:
                try: path.unlink()
                except Exception: pass
            return
        if d=='runrerun':
            rid=c.user_data.get('run_id'); await asyncio.to_thread(gh.rerun,repo_of(c),rid); audit(uid,repo_of(c),'rerun_workflow','ok',rid); return await actions(update,c)
        if d=='runfailed':
            rid=c.user_data.get('run_id'); await asyncio.to_thread(gh.rerun_failed,repo_of(c),rid); audit(uid,repo_of(c),'rerun_failed_jobs','ok',rid); return await actions(update,c)
        if d=='runcancel':
            rid=c.user_data.get('run_id'); await asyncio.to_thread(gh.cancel_run,repo_of(c),rid); audit(uid,repo_of(c),'cancel_workflow','ok',rid); return await actions(update,c)
        if d=='rundel1': return await edit_or_send(update,'⚠️ Hapus workflow run dari history?',kb([[btn('🗑 Hapus Run','rundel2'),btn('✖ Batal','actions')]]))
        if d=='rundel2':
            rid=c.user_data.get('run_id'); await asyncio.to_thread(gh.delete_run,repo_of(c),rid); audit(uid,repo_of(c),'delete_workflow_run','ok',rid); return await actions(update,c)
        if d.startswith('wf:'):
            wid=d.split(':')[1]; r=await asyncio.to_thread(gh.repo,repo_of(c)); c.user_data['wf']=(wid,r['default_branch']); return await edit_or_send(update,f'Jalankan workflow pada <code>{esc(r["default_branch"])}</code>?',kb([[btn('▶️ Jalankan','wfconfirm'),btn('✖ Batal','actions')]]))
        if d=='wfconfirm':
            wid,ref=c.user_data.pop('wf'); await asyncio.to_thread(gh.dispatch,repo_of(c),wid,ref); audit(uid,repo_of(c),'workflow_dispatch','ok',wid); return await edit_or_send(update,'✅ Workflow dispatch dikirim.',kb([[btn('← Actions','actions')]]))
        # Notifications
        if d.startswith('notify:'):
            key=d.split(':',1)[1]; st=state_for(uid); cfg=dict(st.get('notify',{})); cfg[key]=not bool(cfg.get(key)); state_patch(uid,notify=cfg)
            if repo_of(c): state_patch(uid,notify_repo=repo_of(c))
            return await notifications_panel(update,c)
        # Batch
        if d=='batchadd':
            full=repo_of(c)
            if not full: return await edit_or_send(update,'Pilih repo dahulu.',kb([[btn('📦 Repository','repos:0')]]))
            st=state_for(uid); xs=list(st.get('batch_repos',[]));
            if full not in xs: xs.append(full)
            state_patch(uid,batch_repos=xs[:30]); return await batch_panel(update,c)
        if d=='batchclear': state_patch(uid,batch_repos=[]); return await batch_panel(update,c)
        if d=='batchstatus':
            xs=state_for(uid).get('batch_repos',[]); lines=[]
            for rr in xs:
                p=LOCAL_BASE/rr.split('/')[-1]
                if (p/'.git').exists():
                    env=os.environ.copy(); tok=active_token(); env['GH_TOKEN']=tok; env['GITHUB_TOKEN']=tok
                    r=await asyncio.to_thread(subprocess.run,['git','-C',str(p),'status','-sb'],capture_output=True,text=True,timeout=30,env=env); lines.append(f'<b>{esc(rr)}</b>\n<pre>{esc((r.stdout or r.stderr).strip())}</pre>')
                else: lines.append(f'<b>{esc(rr)}</b> — belum clone')
            return await edit_or_send(update,'<b>Batch Status</b>\n\n'+('\n'.join(lines) or 'Tidak ada repo batch.'),kb([[btn('← Batch','batch')]]))
        if d=='batchpull':
            xs=state_for(uid).get('batch_repos',[]); ok=0; fail=[]
            for rr in xs:
                p=LOCAL_BASE/rr.split('/')[-1]
                if not (p/'.git').exists(): fail.append(rr+' (belum clone)'); continue
                env=os.environ.copy(); tok=active_token(); env['GH_TOKEN']=tok; env['GITHUB_TOKEN']=tok
                dirty=await asyncio.to_thread(subprocess.run,['git','-C',str(p),'status','--porcelain'],capture_output=True,text=True,timeout=30,env=env)
                if dirty.stdout.strip(): fail.append(rr+' (dirty)'); continue
                r=await asyncio.to_thread(subprocess.run,['git','-c','credential.helper=!gh auth git-credential','-C',str(p),'pull','--ff-only'],capture_output=True,text=True,timeout=120,env=env)
                if r.returncode: fail.append(rr+' (pull gagal)')
                else: ok+=1
            audit(uid,'batch','batch_pull','ok',f'ok={ok},fail={len(fail)}'); return await edit_or_send(update,f'<b>Batch Pull selesai</b>\nBerhasil: <b>{ok}</b>\nGagal/skip: <b>{len(fail)}</b>\n'+('\n'.join('• '+esc(x) for x in fail[:20]) if fail else ''),kb([[btn('← Batch','batch')]]))
        # Local Git/File manager
        if d=='clone': return await clone(update,c)
        if d=='files': return await file_manager(update,c)
        if d.startswith('fmpage:'): return await file_manager(update,c,None,int(d.split(':')[1]))
        if d.startswith('fmopen:'): return await file_detail(update,c,int(d.split(':')[1]))
        if d=='fmup':
            root,cur=safe_repo_path(c,c.user_data.get('fm_dir','.')); parent=cur.parent if cur!=root else root
            return await file_manager(update,c,str(parent.relative_to(root)) if parent!=root else '.',0)
        if d=='fmdownload': return await send_current_file(update,c)
        if d=='fmdelete1':
            p=Path(c.user_data.get('fm_file','')); root=local_path(c).resolve()
            return await edit_or_send(update,f'⚠️ Hapus file <code>{esc(p.relative_to(root))}</code> dari working tree? Safety branch dibuat dulu.',kb([[btn('🛟 Backup + Hapus','fmdelete2'),btn('✖ Batal','files')]]))
        if d=='fmdelete2':
            await safety_backup(update,c,False); p=Path(c.user_data.pop('fm_file','')).resolve(); root=local_path(c).resolve()
            if not p.exists() or not str(p).startswith(str(root)+os.sep) or '.git' in p.relative_to(root).parts: raise ValueError('File tidak valid')
            if not p.is_file(): raise ValueError('Hanya file yang dapat dihapus dari menu ini')
            rel=str(p.relative_to(root)); p.unlink(); audit(uid,repo_of(c),'delete_file','ok',rel); return await file_manager(update,c)
        if d=='fmmulti':
            await ensure_local_repo(update,c,False); c.user_data.setdefault('fm_dir','.'); c.user_data['await_multi']=True
            return await edit_or_send(update,f'📤 Kirim beberapa file sebagai Document.\nFolder: <code>{esc(c.user_data.get("fm_dir","."))}</code>.',kb([[btn('✅ Selesai Multi Upload','fmmultidone')]]))
        if d=='fmmultidone':
            c.user_data.pop('await_multi',None); c.user_data.pop('multi_backup_done',None); return await file_manager(update,c)
        if d=='fmmedia':
            await ensure_local_repo(update,c,False); c.user_data.setdefault('fm_dir','.'); c.user_data['await_media']=True
            return await edit_or_send(update,f'🖼 <b>Upload Media</b>\nFolder: <code>{esc(c.user_data.get("fm_dir","."))}</code>\n\nKirim foto, video, audio, voice, animation/GIF, atau video note. Bisa kirim beberapa kali lalu tekan Selesai.',kb([[btn('✅ Selesai Upload Media','fmmediadone')],[btn('✖ Batal','cancelupload')]]))
        if d=='fmmediadone': c.user_data.pop('await_media',None); return await file_manager(update,c)
        if d=='fmzip':
            await ensure_local_repo(update,c,False); c.user_data.setdefault('fm_dir','.'); c.user_data['await_zip']=True
            return await edit_or_send(update,f'📦 Kirim file <b>.zip</b>. Isi diekstrak aman ke <code>{esc(c.user_data.get("fm_dir","."))}</code>.',kb([[btn('✖ Batal','cancelupload')]]))
        if d=='tags': return await tags_panel(update,c)
        if d=='diffmenu' or d=='gitdiff': return await diff_menu(update,c)
        if d=='gitstatus': return await do_git(update,c,'status')
        if d=='gitlog': return await do_git(update,c,'log')
        if d=='gitbranch': return await do_git(update,c,'branch')
        if d=='gitpull': return await do_git(update,c,'pull')
        if d=='gitfetch': return await do_git(update,c,'fetch')
        if d=='autosync': return await auto_sync(update,c)
        if d=='safetybackup': return await safety_backup(update,c,True)
        if d=='conflicts': return await conflict_helper(update,c)
        if d=='readme': return await readme_panel(update,c)
        if d=='discardconfirm':
            rel=c.user_data.pop('pending_discard',None)
            if not rel: raise ValueError('Data discard kedaluwarsa')
            await safety_backup(update,c,False)
            try: await asyncio.to_thread(safe_git,c,'restore','--staged','--worktree','--',rel)
            except Exception: await asyncio.to_thread(safe_git,c,'checkout','--',rel)
            audit(uid,repo_of(c),'discard_file','ok',rel); return await diff_menu(update,c)
        if d=='gitstash':
            out=await asyncio.to_thread(safe_git,c,'stash','push','-u','-m','Telegram GitHub Bot stash'); audit(uid,repo_of(c),'stash','ok',out); return await edit_or_send(update,f'<b>Git Stash</b>\n<pre>{esc(out)}</pre>',kb([[btn('← Git / Upload','localrepo')]]))
        if d=='gitstashpop':
            out=await asyncio.to_thread(safe_git,c,'stash','pop'); audit(uid,repo_of(c),'stash_pop','ok',out); return await edit_or_send(update,f'<b>Stash Pop</b>\n<pre>{esc(out)}</pre>',kb([[btn('← Git / Upload','localrepo')]]))
        if d=='confcontinue':
            p=local_path(c)
            if (p/'.git/MERGE_HEAD').exists(): out=await asyncio.to_thread(safe_git,c,'commit','--no-edit')
            elif (p/'.git/CHERRY_PICK_HEAD').exists(): out=await asyncio.to_thread(safe_git,c,'cherry-pick','--continue')
            elif (p/'.git/REVERT_HEAD').exists(): out=await asyncio.to_thread(safe_git,c,'revert','--continue')
            elif (p/'.git/rebase-merge').exists() or (p/'.git/rebase-apply').exists(): out=await asyncio.to_thread(safe_git,c,'rebase','--continue')
            else: raise ValueError('Tidak ada operasi conflict aktif')
            audit(uid,repo_of(c),'conflict_continue','ok',out); return await conflict_helper(update,c)
        if d=='confabort':
            p=local_path(c)
            if (p/'.git/MERGE_HEAD').exists(): out=await asyncio.to_thread(safe_git,c,'merge','--abort')
            elif (p/'.git/CHERRY_PICK_HEAD').exists(): out=await asyncio.to_thread(safe_git,c,'cherry-pick','--abort')
            elif (p/'.git/REVERT_HEAD').exists(): out=await asyncio.to_thread(safe_git,c,'revert','--abort')
            elif (p/'.git/rebase-merge').exists() or (p/'.git/rebase-apply').exists(): out=await asyncio.to_thread(safe_git,c,'rebase','--abort')
            else: raise ValueError('Tidak ada operasi conflict aktif')
            audit(uid,repo_of(c),'conflict_abort','ok',out); return await local_panel(update,c)
        if d=='revertconfirm':
            ref=c.user_data.pop('pending_revert',None)
            if not ref: raise ValueError('Data revert kedaluwarsa')
            await safety_backup(update,c,False); await asyncio.to_thread(safe_git,c,'revert','--no-edit',ref); audit(uid,repo_of(c),'revert','ok',ref); return await edit_or_send(update,'✅ Revert commit berhasil dibuat.',kb([[btn('← Git / Upload','localrepo')]]))
        if d=='resetconfirm':
            pending=c.user_data.pop('pending_reset',None)
            if not pending: raise ValueError('Data reset kedaluwarsa')
            ref,kind=pending; backup=await safety_backup(update,c,False); out=await asyncio.to_thread(safe_git,c,'reset',f'--{kind}',ref); audit(uid,repo_of(c),'reset','ok',f'{kind}:{ref};backup={backup}'); return await edit_or_send(update,f'✅ Reset <b>{kind}</b> ke <code>{esc(ref)}</code> selesai.\nSafety: <code>{esc(backup)}</code>\n<pre>{esc(out)}</pre>',kb([[btn('← Git / Upload','localrepo')]]))
        if d=='deltagconfirm':
            tag=c.user_data.pop('pending_deltag',None)
            if not tag: raise ValueError('Data tag kedaluwarsa')
            await safety_backup(update,c,False)
            try: await asyncio.to_thread(safe_git,c,'tag','-d',tag)
            except Exception: pass
            await asyncio.to_thread(safe_git,c,'push','origin',f':refs/tags/{tag}'); audit(uid,repo_of(c),'delete_tag','ok',tag); return await tags_panel(update,c)
        if d=='gitpush': return await edit_or_send(update,'Konfirmasi <b>git push</b> ke remote?',kb([[btn('⬆️ Push sekarang','gitpush2'),btn('✖ Batal','localrepo')]]))
        if d=='gitpush2':
            out=await asyncio.to_thread(safe_git,c,'push'); audit(uid,repo_of(c),'push','ok',out); return await edit_or_send(update,f'✅ <b>Push selesai</b>\n<pre>{esc(out)}</pre>',kb([[btn('← Git','localrepo')]]))
        if d=='cancelupload':
            for k in ('await_document','upload_target','await_multi','multi_backup_done','await_media','await_zip','await_release_asset','release_asset_tag','await_file_content','create_file_target'): c.user_data.pop(k,None)
            return await local_panel(update,c)
    except Exception as e:
        audit(uid,repo_of(c),d,'error',e)
        await show_error(update,e)

async def cancel_cmd(update,c):
    for k in ('await','await_document','upload_target','await_multi','multi_backup_done','await_media','await_zip','await_release_asset','release_asset_tag','await_file_content','create_file_target','pending_revert','pending_reset','pending_deltag','pending_closeissue','pending_closepr','pending_mergepr','pending_transfer','pending_collabdel','pending_rulesetdel','pending_protect','pending_unprotect','pending_labeldel','pending_milestoneclose','pending_assetdel','pending_discard','delete_repo_full'):
        c.user_data.pop(k,None)
    await update.effective_message.reply_text('Dibatalkan.',reply_markup=kb([[btn('🏠 Menu','home')]]))
async def notification_loop(app):
    await asyncio.sleep(20)
    while True:
        try:
            state=_load_state(); changed=False
            for uid_s,u in list(state.items()):
                acct=str(u.get('github_account') or '1')
                if acct not in GITHUB_ACCOUNTS or not GITHUB_ACCOUNTS[acct].get('token'): continue
                _ACTIVE_GH_ACCOUNT.set(acct)
                cfg=u.get('notify') or {}; full=u.get('notify_repo') or u.get('repo')
                if not full or not any(bool(v) for v in cfg.values()): continue
                snap=dict(u.get('notify_snapshot') or {}); new=dict(snap); msgs=[]
                try:
                    repo=await asyncio.to_thread(gh.repo,full); default=repo.get('default_branch') or 'main'
                    if cfg.get('push'):
                        cs=await asyncio.to_thread(gh.commits,full,default); cur=(cs[0].get('sha') if cs else None)
                        new['push']=cur
                        if snap.get('push') and cur and cur!=snap.get('push'):
                            msg=(cs[0].get('commit') or {}).get('message','').splitlines()[0]
                            msgs.append(f'📤 <b>Push baru</b>\n<code>{esc(full)}</code> • <code>{esc(default)}</code>\n<code>{esc(cur[:8])}</code> {esc(msg)}')
                    if cfg.get('pr'):
                        prs=await asyncio.to_thread(gh.pulls,full); cur=(prs[0].get('id') if prs else 0); new['pr']=cur
                        if snap.get('pr') is not None and cur!=snap.get('pr') and prs:
                            msgs.append(f'🔀 <b>PR baru/berubah</b>\n<code>{esc(full)}</code>\n#{prs[0].get("number")} {esc(prs[0].get("title"))}')
                    if cfg.get('issue'):
                        iss=await asyncio.to_thread(gh.issues,full); cur=(iss[0].get('id') if iss else 0); new['issue']=cur
                        if snap.get('issue') is not None and cur!=snap.get('issue') and iss:
                            msgs.append(f'🧾 <b>Issue baru/berubah</b>\n<code>{esc(full)}</code>\n#{iss[0].get("number")} {esc(iss[0].get("title"))}')
                    if cfg.get('workflow'):
                        runs=await asyncio.to_thread(gh.runs,full); failed=next((x for x in runs if x.get('conclusion')=='failure'),None); cur=(failed.get('id') if failed else 0); new['workflow']=cur
                        if snap.get('workflow') is not None and cur and cur!=snap.get('workflow'):
                            msgs.append(f'❌ <b>Workflow gagal</b>\n<code>{esc(full)}</code>\n{esc(failed.get("name"))} • <code>{esc(failed.get("head_branch"))}</code>')
                    if cfg.get('release'):
                        rels=await asyncio.to_thread(gh.releases,full); cur=(rels[0].get('id') if rels else 0); new['release']=cur
                        if snap.get('release') is not None and cur!=snap.get('release') and rels:
                            msgs.append(f'🚀 <b>Release baru</b>\n<code>{esc(full)}</code>\n<code>{esc(rels[0].get("tag_name"))}</code> — {esc(rels[0].get("name"))}')
                    u['notify_snapshot']=new; changed=True
                    for m in msgs[:5]:
                        try: await app.bot.send_message(chat_id=int(uid_s),text=m,parse_mode='HTML',disable_web_page_preview=True)
                        except Exception: log.exception('notification send failed uid=%s',uid_s)
                except Exception: log.exception('notification check failed repo=%s',full)
            if changed: _save_state(state)
        except Exception: log.exception('notification loop iteration failed')
        await asyncio.sleep(300)

_NOTIFICATION_TASK = None

async def post_init(app):
    global _NOTIFICATION_TASK
    try: await app.bot.set_my_commands([('start','Buka GitHub Control Center'),('menu','Buka menu utama'),('cancel','Batalkan input aktif')])
    except Exception: log.exception('set_my_commands failed')
    _NOTIFICATION_TASK = asyncio.create_task(notification_loop(app),name='github-notification-loop')

async def post_shutdown(app):
    global _NOTIFICATION_TASK
    task=_NOTIFICATION_TASK
    _NOTIFICATION_TASK=None
    if task and not task.done():
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        except Exception: log.exception('notification task shutdown failed')

async def on_error(update,c): log.exception('Unhandled error',exc_info=c.error)

def main():
    missing=[]
    if not BOT_TOKEN: missing.append('TELEGRAM_BOT_TOKEN')
    if not GH_TOKEN: missing.append('GITHUB_TOKEN')
    if not ADMINS: missing.append('TELEGRAM_ADMIN_IDS')
    if missing: raise SystemExit('Konfigurasi belum lengkap di .env: '+', '.join(missing))
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler(['start','menu'],start)); app.add_handler(CommandHandler('cancel',cancel_cmd)); app.add_handler(CallbackQueryHandler(callbacks)); app.add_handler(MessageHandler(filters.Document.ALL,document_input)); app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.ANIMATION | filters.VIDEO_NOTE,media_input)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_input)); app.add_error_handler(on_error)
    log.info('GitHub Control Bot starting; admins=%s',sorted(ADMINS)); app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=False)

if __name__=='__main__': main()
