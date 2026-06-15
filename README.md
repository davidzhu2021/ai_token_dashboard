# AI 鐢ㄩ噺涓績

杩欐槸涓€涓腑鏂?AI Token 鐢ㄩ噺鏌ヨ绯荤粺锛屽綋鍓嶅凡鎺ュ叆鐙珛 FastAPI 鍚庣锛屽苟鎸夊叕鍙哥幇鏈?**Casdoor + 椋炰功 SSO** 鏂规鎻愪緵鎵爜鐧诲綍銆傚墠绔彧璋冪敤鏈郴缁熺殑 `/api/*` 鎺ュ彛锛岀鐞嗗憳瀵嗛挜銆丱IDC client secret 鍜岃璇?token 閮藉彧淇濆瓨鍦ㄥ悗绔€?
## 鏈湴鍚姩

```powershell
cd D:\ai-token-dashboard
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

鎵撳紑锛?
```text
http://127.0.0.1:8000
```

## 椋炰功鎵爜鐧诲綍

鍦?Casdoor 鐨?`cltx` organization 涓嬩负 AI 鐢ㄩ噺涓績鏂板缓鐙珛 application锛?
```text
Application name: ai-token-dashboard
Organization: cltx
Provider: 鐜版湁椋炰功 Provider
Redirect URI: https://ai-usage.auto-link.com.cn/api/auth/callback
```

鍚庣 `.env` 鎺ㄨ崘閰嶇疆锛?
```text
LITELLM_BASE_URL=https://cc.auto-link.com.cn/pro
LITELLM_ADMIN_KEY=<绠＄悊鍛樺瘑閽ワ紝浠呭悗绔繚瀛?

APP_BASE_URL=https://ai-usage.auto-link.com.cn
SESSION_SECRET=<闅忔満闀垮瓧绗︿覆>

OIDC_ISSUER_URL=http://10.68.13.198:30882
OIDC_CLIENT_ID=<ai-token-dashboard client id>
OIDC_CLIENT_SECRET=<ai-token-dashboard client secret>
OIDC_CASDOOR_APPLICATION_ID=admin/ai-token-dashboard
OIDC_DIRECT_PROVIDER=lark-provider
OIDC_DIRECT_METHOD=signup
OIDC_SKIP_CASDOOR_PAGE=true
OIDC_PROVIDER_LOGIN_HOST=accounts.feishu.cn
OAUTH_PROVIDER_NAME=飞书扫码登录
ALLOWED_EMAIL_DOMAIN=auto-link.com.cn

DEV_LOGIN_ENABLED=false
DEBUG_MAPPING_ENABLED=false
DEBUG_OIDC_CLAIMS=false
USAGE_LOG_MAX_PAGES=20
```

濡傞渶鐐瑰嚮鈥滈涔︽壂鐮佺櫥褰曗€濆悗鐩磋揪椋炰功椤甸潰锛宍OIDC_DIRECT_PROVIDER` 闇€瑕佷笌 Casdoor 涓殑椋炰功 Provider 鍚嶇О涓€鑷达紱鍚庣浼氭妸瀹冧綔涓?`provider_hint` 浼犵粰 Casdoor銆傚鏋?Casdoor 宸茬粡鏈?HTTPS 鍙嶄唬鍦板潃锛宍OIDC_ISSUER_URL` 浼樺厛浣跨敤 HTTPS 鍦板潃銆傚悗绔悓鏃跺吋瀹?issuer base URL 鍜屽畬鏁?discovery URL锛?
```text
https://casdoor.example.com
https://casdoor.example.com/.well-known/openid-configuration
```

鏈湴寮€鍙戦獙璇佺湡瀹炴暟鎹椂鍙复鏃跺惎鐢?`DEV_LOGIN_ENABLED=true`锛涚敓浜х幆澧冨繀椤诲叧闂€?
## 宸插疄鐜版帴鍙?
```text
GET  /api/auth/config
GET  /api/auth/me
GET  /api/auth/sso/start
GET  /api/auth/callback
POST /api/auth/logout
POST /api/auth/dev-login
GET  /api/me/usage?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&source=all
GET  /api/me/usage/logs?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&page=1&page_size=50
GET  /api/me/keys
POST /api/me/keys/{key_id}/regenerate
GET  /api/models
GET  /api/debug/me-mapping  # only when DEBUG_MAPPING_ENABLED=true
```

## 鏁版嵁璇存槑

- 椋炰功鎵爜鐧诲綍鍚庯紝鍚庣鍙繚瀛樻渶灏?session 淇℃伅锛氶偖绠便€佸鍚嶃€佸ご鍍忛瀛楁瘝銆?- 鍙湁 `ALLOWED_EMAIL_DOMAIN` 鎸囧畾鐨勫叕鍙搁偖绠卞厑璁哥櫥褰曘€?- 鍛樺伐韬唤閫氳繃閭鍖归厤涓婃父鐢ㄦ埛鐨?`user_email`銆乣sso_user_id` 鎴?`user_id`銆?- 鐢ㄩ噺鏁版嵁浼樺厛鎸変釜浜鸿闂瘑閽ユ煡璇㈡棩鑱氬悎锛屽啀鍥為€€鍒版槑缁嗘棩蹇楀拰鐢ㄦ埛鏃ヨ仛鍚堟帴鍙ｃ€?- 璁块棶瀵嗛挜鏉ヨ嚜 `/key/list?user_id=<褰撳墠鍛樺伐>&return_full_object=true`锛屽墠绔彧灞曠ず鑴辨晱鍊笺€?- 妯″瀷骞垮満鏉ヨ嚜 `/models`锛屽墠绔睍绀哄悗绔繑鍥炵殑褰撳墠璐﹀彿鍙敤妯″瀷銆?
## 瀹夊叏绾︽潫

- 绠＄悊鍛樺瘑閽ヤ笉寰楄繘鍏ュ墠绔唬鐮併€佹祻瑙堝櫒瀛樺偍鎴栨棩蹇椼€?- OIDC `client_secret`銆佽璇?token銆乮d_token 涓嶅緱杩涘叆鍓嶇浠ｇ爜銆佹祻瑙堝櫒瀛樺偍鎴栨棩蹇椼€?- 鍓嶇涓嶈兘浼犱换鎰?`user_id` 鏌ヨ鏁版嵁锛屽悗绔缁堜粠褰撳墠浼氳瘽璇嗗埆鍛樺伐銆?- 鏇存柊璁块棶瀵嗛挜鍓嶏紝鍚庣浼氭牎楠岃瀵嗛挜灞炰簬褰撳墠鍛樺伐銆?- 瀹屾暣鏂板瘑閽ュ彧鍦ㄦ洿鏂板悗杩斿洖涓€娆°€?- 鐢熶骇鐜蹇呴』浣跨敤椋炰功鎵爜鐧诲綍锛屽苟淇濇寔 `DEV_LOGIN_ENABLED=false`銆?- `.env`銆佽櫄鎷熺幆澧冨拰瀹¤鏃ュ織宸插姞鍏?`.gitignore`銆?
## 绠＄悊鍛樺叏鍛樼湅鏉?
鍦ㄥ悗绔?`.env` 涓厤缃鐞嗗憳閭鐧藉悕鍗曞悗锛屽搴斿憳宸ラ涔︾櫥褰曚細鐪嬪埌鈥滃叏鍛樼湅鏉库€濓細

```text
ADMIN_EMAILS=zhuyida@auto-link.com.cn,leader@auto-link.com.cn
ADMIN_USAGE_LOG_MAX_PAGES=30
ADMIN_USAGE_PAGE_SIZE=100
```

绠＄悊鍛樻帴鍙ｏ細

```text
GET /api/admin/usage?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&source=all&employee=
GET /api/admin/users?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&source=all&q=
```

璇存槑锛?
- 绠＄悊鍛樿韩浠藉彧鐢卞悗绔?`.env` 鐨?`ADMIN_EMAILS` 鍒ゆ柇锛屽墠绔笉鑳借嚜琛屽０鏄庣鐞嗗憳銆?- `/api/admin/*` 蹇呴』鐧诲綍涓斿睘浜庣鐞嗗憳鐧藉悕鍗曪紝鏅€氬憳宸ヨ闂繑鍥?403銆?- 鍏ㄥ憳鐪嬫澘鍙繑鍥炶仛鍚堢粺璁°€佸憳宸ュ鍚?閭銆佹ā鍨嬪拰宸ュ叿鏉ユ簮锛屼笉杩斿洖 prompt/response 鍐呭锛屼篃涓嶈繑鍥?`sk-...` 瀵嗛挜銆?- 鍏ㄥ憳鏃ュ織鏉ヨ嚜涓婃父 `/spend/logs/v2`锛宍ADMIN_USAGE_LOG_MAX_PAGES` 鍜?`ADMIN_USAGE_PAGE_SIZE` 鐢ㄤ簬闄愬埗棣栫増鏌ヨ瑙勬ā锛岄伩鍏嶄竴娆¤鍙栬繃澶с€?




## ??????

?????????????????????????????????????

```text
USER_MAPPING_CACHE_TTL_SECONDS=1800
PERSONAL_USAGE_CACHE_TTL_SECONDS=300
```

???

- ????????????????????????????????
- ???????????????????????????? 5 ???
- ??????????????????????? token ? `sk-...` ???
- ???????????????????????? SQLite ? Redis?
