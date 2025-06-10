from urllib.parse import urlencode

import aiohttp_cors
import discord.utils
from aiohttp import ClientSession, web
from aiohttplimiter import Limiter, default_keyfunc
from discord.ext.ipc import Client
from discord.http import Route as DiscordRoute
from discord.types.appinfo import PartialAppInfo
from discord.types.user import User as DiscordUser
from typing_extensions import TypedDict

from config import dbl_secret, ipc_secret, OAuth as OAuthConfig, website

routes = web.RouteTableDef()
ipc = Client(secret_key=ipc_secret)
limiter = Limiter(keyfunc=default_keyfunc)
session = ClientSession()


@routes.get('/')
async def hello(_request: web.Request) -> web.Response:
    return web.Response(text='Hello, world!')


@routes.post('/dbl')
async def dbl(request: web.Request) -> web.Response:
    if request.headers.get('Authorization') != dbl_secret:
        raise web.HTTPUnauthorized()

    data = await request.json()
    # documented as isWeekend but is actually is_weekend
    is_weekend = data.get('is_weekend') or data.get('isWeekend') or False
    await ipc.request(
        'dbl_vote',
        user_id=int(data['user']),
        type=data['type'],
        is_weekend=is_weekend,
        voted_at=discord.utils.utcnow().isoformat(),
    )
    return web.Response()


@routes.post('/discordbotlist')
async def discord_bot_list(request: web.Request) -> web.Response:
    if request.headers.get('Authorization') != dbl_secret:
        raise web.HTTPUnauthorized()

    data = await request.json()
    await ipc.request(
        'discordbotlist_vote',
        id=int(data['id']),
        username=data['username'],
        avatar=data['avatar'],
        admin=data['admin'],
    )
    return web.Response()


@routes.get('/global')
@limiter.limit('5/4second')
async def global_(_request: web.Request) -> web.Response:
    response = await ipc.request('global_stats')
    return web.json_response(response.response)


@routes.get('/user/{user_id:\\d+}')
@limiter.limit('2/8second')
async def user_data(request: web.Request) -> web.Response:
    user_id = int(request.match_info['user_id'])
    response = await ipc.request('user_data', user_id=user_id)
    return web.json_response(response.response)


async def oauth_request(endpoint: DiscordRoute, **kwargs) -> dict:
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'client_id': OAuthConfig.client_id,
        'client_secret': OAuthConfig.client_secret,
        **kwargs,
    }
    async with session.request(endpoint.method, endpoint.url, headers=headers, data=urlencode(data)) as response:
        response.raise_for_status()
        return await response.json()


class DiscordAuthorization(TypedDict):
    application: PartialAppInfo
    scopes: list[str]
    expires: str
    user: DiscordUser


async def fetch_oauth_authorization(*, access_token: str) -> DiscordAuthorization:
    headers = {
        'Authorization': f'Bearer {access_token}',
    }
    async with session.get(f'{DiscordRoute.BASE}/oauth2/@me', headers=headers) as response:
        response.raise_for_status()
        return await response.json()


@routes.post('/oauth')
@limiter.limit('5/5second')
async def oauth_exchange_code(request: web.Request) -> web.Response:
    data = await request.json()
    code = data.get('code')
    if not code:
        raise web.HTTPBadRequest(text='Must have `code` in JSON body')

    response = await oauth_request(
        DiscordRoute('POST', '/oauth2/token'),
        grant_type='authorization_code', code=code, redirect_uri=website
    )
    if 'access_token' not in response:
        raise web.HTTPServerError(text='Response did not contain access_token')

    authorization = await fetch_oauth_authorization(access_token=response['access_token'])
    if not {'identify', 'guilds', 'email'}.issubset(set(authorization['scopes'])):
        raise web.HTTPForbidden(text='Insufficient scopes granted by user')

    await ipc.request(
        'oauth_token_update',
        user_id=authorization['user']['id'],
        access_token=response['access_token'],
    )
    return web.json_response({
        'user': authorization['user'],
        'access_token': response['access_token'],
        'refresh_token': response.get('refresh_token'),
        'expires_in': response.get('expires_in', 0),
    })


@routes.patch('/oauth')
@limiter.limit('5/5second')
async def oauth_refresh_token(request: web.Request) -> web.Response:
    data = await request.json()
    user_id = data.get('user_id')
    if not isinstance(user_id, int):
        try:
            user_id = int(user_id)
        except ValueError:
            user_id = None

    refresh_token = data.get('refresh_token')
    if not refresh_token:
        raise web.HTTPBadRequest(text='Must have `refresh_token` in JSON body')

    response = await oauth_request(
        DiscordRoute('POST', '/oauth2/token'),
        grant_type='refresh_token', refresh_token=refresh_token,
    )
    if 'access_token' not in response:
        raise web.HTTPServerError(text='Response did not contain access_token')

    await ipc.request('oauth_token_update', user_id=user_id, access_token=response['access_token'])
    return web.json_response({
        'access_token': response['access_token'],
        'refresh_token': response.get('refresh_token'),
        'expires_in': response.get('expires_in', 0),
    })


@routes.delete('/oauth')
@limiter.limit('5/5second')
async def oauth_revoke_token(request: web.Request) -> web.Response:
    token = request.query.get('token')
    if not token:
        raise web.HTTPBadRequest(text='Must have `token` in query parameters')

    await oauth_request(
        DiscordRoute('POST', '/oauth2/token/revoke'),
        token=token
    )
    return web.Response(text='Token revoked successfully')


@routes.get('/me')
@limiter.limit('3/5second')
async def user_info(request: web.Request) -> web.Response:
    token = request.headers.get('Authorization', '').removeprefix('Bearer ')
    if not token:
        raise web.HTTPUnauthorized(text='Authorization header is required')

    authorization = await fetch_oauth_authorization(access_token=token)
    return web.json_response(authorization['user'])


if __name__ == '__main__':
    app = web.Application()
    app.add_routes(routes)

    cors = aiohttp_cors.setup(app, defaults={
        '*': aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers='*',
            allow_headers='*',
        ),
    })
    for route in list(app.router.routes()):
        cors.add(route)

    web.run_app(app, port=8090)
