from fastapi import FastAPI, Request, HTTPException, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os, json, hashlib, inspect, re
from openai import OpenAI
import json5
from fastmcp import FastMCP
from typing import Optional
import uvicorn
import httpx

# 환경변수
from dotenv import load_dotenv
load_dotenv()

##################################################################
########################## FastAPI Setup #########################
##################################################################

# MCP 서버 설정 (lifespan 사용을 위해 먼저 생성)
mcp = FastMCP("MCP Server")

# tools.py 모듈의 모든 함수를 MCP 툴로 등록
import tools
for name, fn in inspect.getmembers(tools, inspect.isfunction):
    mcp.tool(fn)

# MCP 앱 생성
mcp_app = mcp.http_app()

# FastAPI 앱 생성 (MCP lifespan 연결)
app = FastAPI(lifespan=mcp_app.lifespan)

# health check endpoint
@app.get("/")
async def root(request: Request):
    # return {"status": "ok"}
    title = os.environ.get("TITLE", "🤖 OpenAI API Agent School").strip()
    return templates.TemplateResponse("index.html", {"request": request, "title": title})

@app.get("/health")
async def health_check():
    return {"status": "ok"}

#################################################################
######################## MCP Server #############################
#################################################################

# MCP 툴 목록 표시 (별도 경로)
@app.get("/mcp-tools")
async def mcp_tools_handler(request: Request):
    tools_list = [{"name": name, "description": (fn.__doc__ or "설명 없음").strip()} 
                  for name, fn in inspect.getmembers(tools, inspect.isfunction)]
    return templates.TemplateResponse("mcp.html", {"request": request, "tools": tools_list})


# MCP 비밀번호 보호 미들웨어
if PASSWORD := os.getenv("PASSWORD", ""):
    @app.middleware("http")
    async def mcp_auth_middleware(request: Request, call_next):
        if request.url.path.startswith("/mcp") :
            password_param = request.query_params.get("password")
            if not password_param or password_param != PASSWORD:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

##################################################################
######################## Agent App ###############################
##################################################################

# OpenAI 클라이언트
client = OpenAI() if os.getenv("OPENAI_API_KEY") else None

# Static 파일 서빙 설정
app.mount("/static", StaticFiles(directory="assets/static"), name="static")

# 템플릿 설정
templates = Jinja2Templates(directory="assets/templates")

# CORS 설정 - MCP 통신 지원
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# MCP 앱 마운트 (모든 미들웨어 설정 후)
app.mount("/mcp", mcp_app)

def apply_config_overrides(base_dict, override_dict):
    if not isinstance(override_dict, dict):
        return override_dict
    
    result = base_dict.copy()
    
    for key, value in override_dict.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = apply_config_overrides(result[key], value)
        else:
            result[key] = value
    
    return result

def load_config_overrides():
    override_paths = [
        './config.overrides.jsonc',
        '/etc/secrets/config.overrides.jsonc',
    ]
    
    for path in override_paths:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    content = f.read()
                overrides = json5.loads(content)
                print(f"Loaded config overrides from: {path}")
                return overrides
            except Exception as e:
                print(f"Error loading config overrides from {path}: {e}")
                continue
    
    print("No config overrides found")
    return {}

# config 설정
config = {}
if os.environ.get("PROMPT_ID"):
    config = {"prompt": { "id": os.environ["PROMPT_ID"] }}
    print(f"Using prompt ID from environment: {os.environ['PROMPT_ID']}")
else:
    config = {"model": "gpt-5"}
    print("Using default model: gpt-5")

# config.overrides.jsonc 적용
config_overrides = load_config_overrides()
if config_overrides:
    config = apply_config_overrides(config, config_overrides)
    print(f"Applied config overrides. Final config: {json.dumps(config, indent=2)}")

# 인증 관련 함수들
def generate_auth_token():
    password = os.environ.get('PASSWORD', '').strip()
    return hashlib.md5(f"{password}salt".encode()).hexdigest()

def check_password_required():
    password = os.environ.get('PASSWORD', '').strip()
    return bool(password)

def is_authenticated(auth_token: Optional[str] = None):
    if not check_password_required():
        return True
    return auth_token == generate_auth_token()


# 로그인 페이지
@app.get("/agent/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    if not check_password_required():
        return RedirectResponse(url="/agent", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@app.post("/agent/login")
async def login_submit(password: str = Form(...)):
    if not check_password_required():
        return RedirectResponse(url="/agent", status_code=302)
    
    env_password = os.environ.get('PASSWORD', '').strip()
    if password.strip() == env_password:
        response = RedirectResponse(url="/agent", status_code=302)
        response.set_cookie("auth_token", generate_auth_token(), max_age=60*60*24*30)
        return response
    else:
        return RedirectResponse(url="/agent/login?error=Invalid password", status_code=302)

@app.get("/agent/logout")
async def logout():
    response = RedirectResponse(url="/agent/login", status_code=302)
    response.delete_cookie("auth_token")
    return response

# 메인 페이지
@app.get("/agent", response_class=HTMLResponse)
async def index(request: Request, auth_token: Optional[str] = Cookie(None)):
    if not is_authenticated(auth_token):
        return RedirectResponse(url="/agent/login", status_code=302)
    
    title = os.environ.get("TITLE", "🤖 OpenAI API Agent School").strip()
    return templates.TemplateResponse("agent.html", {
        "request": request,
        "title": title,
        "config": {'PASSWORD': os.environ.get('PASSWORD')}
    })

### 채팅 API 
@app.post("/agent/api")
async def chat_api(request: Request, auth_token: Optional[str] = Cookie(None)):
    if not is_authenticated(auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # OpenAI 클라이언트 검증
    if client is None:
        raise HTTPException(status_code=500, detail="OpenAI API key is not configured. Please set OPENAI_API_KEY environment variable.")
    
    data = await request.json()
    input_message = data.get("input_message", [])
    previous_response_id = data.get("previous_response_id")

    async def generate():
        nonlocal previous_response_id
        try:
            api_params = config.copy()
            api_params.update({
                'input': input_message,
                'previous_response_id': previous_response_id,
                'stream': True
            })
            response = client.responses.create(**api_params)

            max_repeats = 5
            for _ in range(max_repeats):
                follow_up_input = []
                annotations = []
                try:
                    for event in response:
                        print(f"Processing event type: {event.type}")
                        yield f"data: {json.dumps({'type': 'status', 'status': event.type})}\n\n"
                        
                        if event.type == "response.output_text.delta":
                            yield f"data: {json.dumps({'type': 'text_delta', 'delta': event.delta})}\n\n"

                        elif event.type == "response.completed":
                            previous_response_id = event.response.id

                        elif event.type == "response.image_generation_call.partial_image":
                            image_data_url = f"data:image/{event.output_format};base64,{event.partial_image_b64}"
                            yield f"data: {json.dumps({'type': 'image_generated', 'image_data': image_data_url, 'is_partial': True})}\n\n"

                        elif event.type == "response.output_item.done":
                            if event.item.type == "function_call":
                                try:
                                    import tools
                                    func = getattr(tools, event.item.name)
                                    args = json.loads(event.item.arguments)
                                    func_output = str(func(**args))
                                except Exception as e:
                                    func_output = str(e)
                                finally:
                                    follow_up_input.append({
                                        "type": "function_call_output", 
                                        "call_id": event.item.call_id, 
                                        "output": func_output
                                    })
                            elif event.item.type == "mcp_approval_request":
                                    follow_up_input.append({
                                        "type": "mcp_approval_response",
                                        "approval_request_id": event.item.id,
                                        "approve": True
                                    })
                            elif event.item.type == "message":
                                for content in event.item.content:
                                    content_dict = content.model_dump()
                                    if 'annotations' in content_dict:
                                        annotations += content_dict['annotations']
                except Exception as stream_error:
                    print(f"Error in stream processing: {stream_error}")
                    yield f"data: {json.dumps({'type': 'error', 'message': f'Stream error: {str(stream_error)}'})}\n\n"
                    break  # 스트림 에러가 발생하면 루프 중단

                            
                # 함수 호출 결과가 있으면 다시 API 호출
                if follow_up_input:
                    print(f"Making follow-up API call with {len(follow_up_input)}")
                    api_params = config.copy()
                    api_params.update({
                        'input': follow_up_input,
                        'previous_response_id': previous_response_id,
                        'stream': True
                    })
                    response = client.responses.create(**api_params)
                else:
                    break

            yield f"data: {json.dumps({'type': 'done', 'response_id': previous_response_id, 'annotations': annotations})}\n\n"
            print("Stream completed successfully")

        except Exception as e:
            print(f"Error in chat API: {str(e)}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/plain")

# 파일 프록시 엔드포인트 - sandbox 파일을 다운로드할 수 있게 해줌
@app.get("/agent/files/{container_id}/{file_id}")
async def proxy_sandbox_file(container_id: str, file_id: str, filename: str = None, auth_token: Optional[str] = Cookie(None)):
    if not is_authenticated(auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not client:
        raise HTTPException(status_code=500, detail="OpenAI API key is not configured")
    
    try:
        # OpenAI Container Files API 호출
        url = f"https://api.openai.com/v1/containers/{container_id}/files/{file_id}/content"
        headers = {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"}
        
        async with httpx.AsyncClient() as client_http:
            response = await client_http.get(url, headers=headers)
            response.raise_for_status()
            
            # 파일명 결정: 파라미터로 받은 filename이 있으면 사용, 없으면 file_id 사용
            download_filename = filename if filename else file_id
            
            # 파일 내용과 헤더를 클라이언트에게 전달
            content_type = response.headers.get("content-type", "application/octet-stream")
            return Response(
                content=response.content, 
                media_type=content_type,
                headers={
                    "Content-Disposition": f"attachment; filename={download_filename}",
                    "Cache-Control": "public, max-age=3600"
                }
            )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"Failed to fetch file: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error proxying file: {str(e)}")

##################################################################
####################### Server Startup ##########################
##################################################################

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 Starting unified server on port {port}")
    print(f"🤖 Agent App: http://localhost:{port}/agent")
    print(f"🔧 MCP Server: http://localhost:{port}/mcp")
    print(f"🛠️ MCP Tools: http://localhost:{port}/mcp-tools")
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        timeout_keep_alive=0,  # timeout 무제한
        timeout_graceful_shutdown=0,
        access_log=True,
        log_level="info"
    )