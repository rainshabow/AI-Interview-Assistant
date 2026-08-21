import os
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi import Form, File, UploadFile
from fastapi.responses import StreamingResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from passlib.context import CryptContext
from dotenv import load_dotenv

# 加载外部 .env 文件（用户已将其移动到 D:\Code\Other\.env）
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

DATA_FILE = Path(__file__).parent / "data.json"
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'GLM-4-Flash-250414')
TOKEN_TTL_SECONDS = int(os.getenv('TOKEN_TTL_SECONDS', '3600'))

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _load_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        DATA_FILE.write_text(json.dumps({"users": [], "tokens": {}, "positions": [], "sessions": []}, ensure_ascii=False))
    return json.loads(DATA_FILE.read_text())


def _save_data(data: Dict[str, Any]):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _now_iso():
    return datetime.utcnow().isoformat() + 'Z'


class RegisterReq(BaseModel):
    username: str
    email: Optional[str]
    password: str


class LoginReq(BaseModel):
    username: str
    password: str


class PositionReq(BaseModel):
    title: str
    description: Optional[str] = ''


class StartInterviewReq(BaseModel):
    position_id: str
    resume_text: Optional[str] = ''


class MessageReq(BaseModel):
    content: str


class ChatReq(BaseModel):
    session_id: str
    message: str


def _hash_password(pw: str) -> str:
    return pwd_context.hash(pw)


def _verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _make_token() -> str:
    return uuid.uuid4().hex


def _get_user_by_token(token: str):
    data = _load_data()
    token_entry = data.get('tokens', {}).get(token)
    if not token_entry:
        return None
    if token_entry.get('expires_at') and datetime.fromisoformat(token_entry['expires_at'].replace('Z','')) < datetime.utcnow():
        return None
    user_id = token_entry['user_id']
    for u in data['users']:
        if u['id'] == user_id:
            return u
    return None


def _llm_generate(prompt: str, system: Optional[str] = None, kind: Optional[str] = None) -> str:
    # If OPENAI_API_KEY is set, call OpenAI; otherwise return a controlled mock text
    if not OPENAI_API_KEY:
        kind_map = {
            'initial': '初始消息',
            'reply': 'AI 回复',
            'report': '分析报告'
        }
        label = kind_map.get(kind, '模型')
        return f"这是{label}的示例输出"
    try:
        import json
        import urllib.request
        import urllib.error

        # 默认使用智谱开放平台的 chat completions 接口；可通过 BIGMODEL_ENDPOINT 覆盖
        endpoint = os.getenv('BIGMODEL_ENDPOINT', 'https://open.bigmodel.cn/api/paas/v4/chat/completions')

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            'model': OPENAI_MODEL,
            'messages': messages,
            'temperature': 0.2,
            'stream': False
        }

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(endpoint, data=data, headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {OPENAI_API_KEY}'
        }, method='POST')

        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_text = resp.read().decode('utf-8')
            try:
                j = json.loads(resp_text)
            except Exception:
                # 返回非 JSON 时直接返回原文
                return resp_text

        # 兼容不同返回结构，优先读取 choices[0].message.content
        choices = j.get('choices') if isinstance(j, dict) else None
        if choices and len(choices) > 0:
            first = choices[0]
            # OpenAI-like
            if isinstance(first.get('message'), dict) and first['message'].get('content'):
                return first['message']['content']
            # 其他服务可能直接返回 text 或 content
            if first.get('content'):
                return first.get('content')
            if first.get('text'):
                return first.get('text')

        # 兼容部分厂商把结果放 data 或 result 字段
        if isinstance(j, dict):
            if 'data' in j and isinstance(j['data'], list) and len(j['data']) > 0:
                d0 = j['data'][0]
                if isinstance(d0, dict) and d0.get('content'):
                    return d0.get('content')
            if j.get('result'):
                return j.get('result')

        return resp_text
    except Exception as e:
        return f"模型调用失败：{e}"


def _require_auth(authorization: Optional[str] = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if not authorization.lower().startswith('bearer '):
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = authorization.split(' ', 1)[1]
    user = _get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


@app.post('/api/auth/register')
def register(req: RegisterReq):
    data = _load_data()
    # simple uniqueness check on username
    if any(u['username'] == req.username for u in data['users']):
        raise HTTPException(status_code=400, detail='用户名已存在')
    user = {
        'id': uuid.uuid4().hex,
        'username': req.username,
        'email': req.email or '',
        'password': _hash_password(req.password),
        'created_at': _now_iso()
    }
    data['users'].append(user)
    token = _make_token()
    data.setdefault('tokens', {})[token] = {'user_id': user['id'], 'issued_at': _now_iso(), 'expires_at': (datetime.utcnow() + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat() + 'Z'}
    _save_data(data)
    return {'access_token': token, 'user': {'id': user['id'], 'username': user['username'], 'email': user['email']}}


@app.post('/api/auth/login')
def login(req: LoginReq):
    data = _load_data()
    user = next((u for u in data['users'] if u['username'] == req.username), None)
    if not user or not _verify_password(req.password, user['password']):
        raise HTTPException(status_code=400, detail='用户名或密码错误')
    token = _make_token()
    data.setdefault('tokens', {})[token] = {'user_id': user['id'], 'issued_at': _now_iso(), 'expires_at': (datetime.utcnow() + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat() + 'Z'}
    _save_data(data)
    return {'access_token': token, 'user': {'id': user['id'], 'username': user['username'], 'email': user['email']}}


@app.post('/api/auth/token')
def token(username: str = Form(...), password: str = Form(...)):
    # 支持前端以 application/x-www-form-urlencoded 提交的登录（兼容旧端）
    data = _load_data()
    user = next((u for u in data['users'] if u['username'] == username), None)
    if not user or not _verify_password(password, user['password']):
        raise HTTPException(status_code=400, detail='用户名或密码错误')
    token = _make_token()
    data.setdefault('tokens', {})[token] = {'user_id': user['id'], 'issued_at': _now_iso(), 'expires_at': (datetime.utcnow() + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat() + 'Z'}
    _save_data(data)
    return {'access_token': token, 'user': {'id': user['id'], 'username': user['username'], 'email': user['email']}}


@app.get('/api/auth/me')
def me(user=Depends(_require_auth)):
    # 返回当前用户信息（不包含密码）
    return {'id': user['id'], 'username': user['username'], 'email': user.get('email', '')}


@app.get('/api/positions')
def list_positions():
    data = _load_data()
    # 兼容前端字段：返回 position_id 和 jd_text
    positions = []
    for p in data.get('positions', []):
        # try to resolve creator username
        creator = next((u for u in data.get('users', []) if u.get('id') == p.get('created_by')), None)
        positions.append({
            'id': p.get('id'),
            'position_id': p.get('id'),
            'title': p.get('title'),
            'description': p.get('description', ''),
            'jd_text': p.get('description', ''),
            'created_at': p.get('created_at'),
            'created_by': p.get('created_by'),
            'created_by_username': creator.get('username') if creator else ''
        })
    return positions


@app.post('/api/positions')
def create_position(req: PositionReq, user=Depends(_require_auth)):
    data = _load_data()
    pos_id = uuid.uuid4().hex
    position = {'id': pos_id, 'position_id': pos_id, 'title': req.title, 'description': req.description or '', 'jd_text': req.description or '', 'created_at': _now_iso(), 'created_by': user['id'], 'created_by_username': user.get('username')}
    data.setdefault('positions', []).append(position)
    _save_data(data)
    return position


@app.get('/api/positions/{position_id}')
def get_position(position_id: str):
    data = _load_data()
    p = next((p for p in data.get('positions', []) if p['id'] == position_id), None)
    if not p:
        pos = None
    else:
        creator = next((u for u in data.get('users', []) if u.get('id') == p.get('created_by')), None)
        pos = {
            'id': p.get('id'),
            'position_id': p.get('id'),
            'title': p.get('title'),
            'description': p.get('description', ''),
            'jd_text': p.get('description', ''),
            'created_at': p.get('created_at'),
            'created_by': p.get('created_by'),
            'created_by_username': creator.get('username') if creator else ''
        }
    if not pos:
        raise HTTPException(status_code=404, detail='岗位未找到')
    return pos


@app.delete('/api/positions/{position_id}')
def delete_position(position_id: str, user=Depends(_require_auth)):
    data = _load_data()
    idx = next((i for i, p in enumerate(data.get('positions', [])) if p.get('id') == position_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail='岗位未找到')
    # Only allow creator to delete (simple rule)
    pos = data['positions'][idx]
    if pos.get('created_by') != user['id']:
        raise HTTPException(status_code=403, detail='无权限删除该岗位')
    data['positions'].pop(idx)
    # Optionally remove related sessions
    data['sessions'] = [s for s in data.get('sessions', []) if s.get('position_id') != position_id]
    _save_data(data)
    return {'detail': '删除成功'}


@app.post('/api/interview/start')
async def start_interview(position_id: str = Form(...), cv_text: Optional[str] = Form(None), cv_file: UploadFile = File(None), user=Depends(_require_auth)):
    data = _load_data()
    # validate position
    pos = next((p for p in data.get('positions', []) if p['id'] == position_id), None)
    if not pos:
        raise HTTPException(status_code=400, detail='无效的 position_id')

    # assemble resume text from form field and optional uploaded file
    resume_text = cv_text or ''
    if cv_file is not None:
        try:
            content = await cv_file.read()
            text = content.decode('utf-8', errors='ignore')
            if resume_text:
                resume_text = resume_text + "\n" + text
            else:
                resume_text = text
        except Exception:
            # fallback: ignore file content if cannot be read
            pass

    session_id = uuid.uuid4().hex
    # generate initial message via LLM (or mock)
    system = f"你是面试官，当前岗位：{pos['title']}。请以礼貌的方式开启面试，并给出首句欢迎语。"
    prompt = f"简短的欢迎语并引导候选人自我介绍。岗位描述：{pos.get('description','')}. 简历摘要：{(resume_text or '')[:1000]}"
    initial_message = _llm_generate(prompt, system=system, kind='initial')

    session = {
        'session_id': session_id,
        'user_id': user['id'],
        'position_id': position_id,
        'is_ended': False,
        'created_at': _now_iso(),
        'initial_message': initial_message,
        'messages': [
            {'role': 'ai', 'content': initial_message, 'created_at': _now_iso()}
        ]
    }
    data.setdefault('sessions', []).append(session)
    _save_data(data)
    # return as the frontend expects
    return session


@app.get('/api/interview/sessions')
def list_sessions(user=Depends(_require_auth)):
    data = _load_data()
    return [s for s in data.get('sessions', []) if s['user_id'] == user['id']]


@app.get('/api/interview/sessions/{session_id}/messages')
def get_messages(session_id: str, user=Depends(_require_auth)):
    data = _load_data()
    s = next((s for s in data.get('sessions', []) if s['session_id'] == session_id and s['user_id'] == user['id']), None)
    if not s:
        raise HTTPException(status_code=404, detail='会话未找到')
    return s.get('messages', [])


@app.delete('/api/interview/sessions/{session_id}')
def delete_session(session_id: str, user=Depends(_require_auth)):
    data = _load_data()
    idx = next((i for i, s in enumerate(data.get('sessions', [])) if s.get('session_id') == session_id and s.get('user_id') == user['id']), None)
    if idx is None:
        raise HTTPException(status_code=404, detail='会话未找到')
    data['sessions'].pop(idx)
    _save_data(data)
    return {'detail': '会话已删除'}


@app.post('/api/interview/sessions/{session_id}/messages')
def post_message(session_id: str, req: MessageReq, user=Depends(_require_auth)):
    data = _load_data()
    s = next((s for s in data.get('sessions', []) if s['session_id'] == session_id and s['user_id'] == user['id']), None)
    if not s:
        raise HTTPException(status_code=404, detail='会话未找到')
    # append user message
    user_msg = {'role': 'human', 'content': req.content, 'created_at': _now_iso()}
    s.setdefault('messages', []).append(user_msg)
    # generate AI reply
    system = f"你是面试官，针对岗位 {s['position_id']} 进行自然语言面试回复。回答应简洁、专业。"
    prompt = f"候选人说：{req.content}\n请作为面试官回复一段话。"
    ai_text = _llm_generate(prompt, system=system, kind='reply')
    ai_msg = {'role': 'ai', 'content': ai_text, 'created_at': _now_iso()}
    s.setdefault('messages', []).append(ai_msg)
    _save_data(data)
    return ai_msg


@app.post('/api/interview/chat')
async def chat_endpoint(req: ChatReq, user=Depends(_require_auth)):
    data = _load_data()
    s = next((s for s in data.get('sessions', []) if s['session_id'] == req.session_id and s['user_id'] == user['id']), None)
    if not s:
        raise HTTPException(status_code=404, detail='会话未找到')

    # append user message immediately
    user_msg = {'role': 'human', 'content': req.message, 'created_at': _now_iso()}
    s.setdefault('messages', []).append(user_msg)
    _save_data(data)

    # generate AI reply (synchronously) then stream it in chunks as SSE
    system = f"你是面试官，针对岗位 {s['position_id']} 进行自然语言面试回复。回答应简洁、专业。"
    prompt = f"候选人说：{req.message}\n请作为面试官回复一段话。"
    ai_text = _llm_generate(prompt, system=system, kind='reply')

    # append AI message to session
    ai_msg = {'role': 'ai', 'content': ai_text, 'created_at': _now_iso()}
    s.setdefault('messages', []).append(ai_msg)
    _save_data(data)

    async def event_generator(text: str):
        # split into reasonable chunks for frontend stream handling
        chunk_size = 256
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i+chunk_size]
            payload = json.dumps({'chunk': chunk})
            yield f"data: {payload}\n\n"
            await asyncio.sleep(0.01)
        # signal completion
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(event_generator(ai_text), media_type='text/event-stream')


@app.get('/api/interview/sessions/{session_id}/report')
def get_report(session_id: str, user=Depends(_require_auth)):
    data = _load_data()
    s = next((s for s in data.get('sessions', []) if s['session_id'] == session_id and s['user_id'] == user['id']), None)
    if not s:
        raise HTTPException(status_code=404, detail='会话未找到')
    # Compose brief report prompt
    transcript = '\n'.join([f"[{m['role']}] {m['content']}" for m in s.get('messages', [])])
    prompt = f"请根据以下面试对话生成一份面试评估报告（要点、评分建议、改进建议），以 Markdown 格式输出：\n\n{transcript[:4000]}"
    report = _llm_generate(prompt, system="你是面试评估助手，输出 Markdown 格式的评估报告。", kind='report')
    return {'session_id': session_id, 'report': report}


@app.post('/api/interview/analyze')
async def analyze_endpoint(payload: dict, user=Depends(_require_auth)):
    # 接受 JSON { session_id }
    session_id = payload.get('session_id')
    if not session_id:
        raise HTTPException(status_code=400, detail='missing session_id')
    data = _load_data()
    s = next((s for s in data.get('sessions', []) if s['session_id'] == session_id and s['user_id'] == user['id']), None)
    if not s:
        raise HTTPException(status_code=404, detail='会话未找到')

    transcript = '\n'.join([f"[{m['role']}] {m['content']}" for m in s.get('messages', [])])
    prompt = f"请根据以下面试对话生成一份面试评估报告（要点、评分建议、改进建议），以 Markdown 格式输出：\n\n{transcript[:4000]}"
    report = _llm_generate(prompt, system="你是面试评估助手，输出 Markdown 格式的评估报告。", kind='report')

    async def gen(text: str):
        chunk_size = 512
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i+chunk_size]
            payload = json.dumps({'chunk': chunk})
            yield f"data: {payload}\n\n"
            await asyncio.sleep(0.01)
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(gen(report), media_type='text/event-stream')


@app.post('/api/interview/sessions/{session_id}/end')
def end_session(session_id: str, user=Depends(_require_auth)):
    """Mark a session as ended, generate and save an analysis report to the session."""
    data = _load_data()
    s = next((s for s in data.get('sessions', []) if s['session_id'] == session_id and s['user_id'] == user['id']), None)
    if not s:
        raise HTTPException(status_code=404, detail='会话未找到')

    # If already ended and has report, return it
    if s.get('is_ended') and s.get('report'):
        return s

    # Compose transcript and generate report
    transcript = '\n'.join([f"[{m['role']}] {m['content']}" for m in s.get('messages', [])])
    prompt = f"请根据以下面试对话生成一份面试评估报告（要点、评分建议、改进建议），以 Markdown 格式输出：\n\n{transcript[:4000]}"
    report = _llm_generate(prompt, system="你是面试评估助手，输出 Markdown 格式的评估报告。", kind='report')

    s['report'] = report
    s['is_ended'] = True
    s['report_generated_at'] = _now_iso()
    _save_data(data)
    return s


if __name__ == "__main__":
    # Allow starting the app directly with `python main.py`.
    # Default host/port can be overridden via environment variables: HOST, PORT, RELOAD.
    try:
        import uvicorn
    except Exception:
        print("uvicorn 未安装。请运行: pip install 'uvicorn[standard]' 后重试")
        raise

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    # By default do not enable auto-reload when using direct run (reload spawns subprocesses).
    reload_env = os.getenv("RELOAD")
    if reload_env is None:
        reload = False
    else:
        reload = reload_env.lower() in ("1", "true", "yes")

    uvicorn.run(app, host=host, port=port, reload=reload)
