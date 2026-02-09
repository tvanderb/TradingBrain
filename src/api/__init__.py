from aiohttp import web

api_key_key = web.AppKey("api_key", str)
ctx_key = web.AppKey("ctx", dict)
