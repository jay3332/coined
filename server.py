from urllib.parse import urlencode

import aiohttp_cors
import discord.utils
from aiohttp import ClientSession, web
from aiohttplimiter import Limiter, default_keyfunc
from discord.ext.ipc import Client
from discord.http import Route as DiscordRoute
from discord.types.appinfo import PartialAppInfo
from discord.types.user import User as DiscordUser
from stripe import AIOHTTPClient as StripeAiohttp, StripeClient
from typing_extensions import TypedDict

from config import (
    dbl_secret,
    ipc_secret,
    OAuth as OAuthConfig,
    stripe_api_key,
    StripeSKUs,
    website,
)

routes = web.RouteTableDef()
ipc = Client(secret_key=ipc_secret)
limiter = Limiter(keyfunc=default_keyfunc)


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


async def authenticated_discord_request(session: ClientSession, endpoint: DiscordRoute, **kwargs) -> dict:
    headers = {
        'Content-Type': 'application/json',
    }
    token = kwargs.pop('token', None)
    if token:
        headers['Authorization'] = f'Bearer {token}'

    async with session.request(endpoint.method, endpoint.url, headers=headers, **kwargs) as response:
        response.raise_for_status()
        return await response.json()


async def oauth_request(session: ClientSession, endpoint: DiscordRoute, **kwargs) -> dict:
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


async def fetch_oauth_authorization(session: ClientSession, *, access_token: str) -> DiscordAuthorization:
    headers = {
        'Authorization': f'Bearer {access_token}',
    }
    async with session.get(f'{DiscordRoute.BASE}/oauth2/@me', headers=headers) as response:
        response.raise_for_status()
        return await response.json()


async def update_access_token(session: ClientSession, *, data: dict) -> web.Response:
    access_token = data['access_token']
    authorization = await fetch_oauth_authorization(session, access_token=access_token)
    if not {'identify', 'guilds', 'email'}.issubset(set(authorization['scopes'])):
        raise web.HTTPForbidden(text='Insufficient scopes granted by user')

    await ipc.request(
        'oauth_token_update',
        user_id=authorization['user']['id'],
        email=authorization['user'].get('email'),
        access_token=data,
    )
    return web.json_response({
        'user': authorization['user'],
        'access_token': access_token,
        'refresh_token': data.get('refresh_token'),
        'expires_in': data.get('expires_in', 0),
    })


@routes.post('/oauth')
@limiter.limit('5/5second')
async def oauth_exchange_code(request: web.Request) -> web.Response:
    data = await request.json()
    code = data.get('code')
    if not code:
        raise web.HTTPBadRequest(text='Must have `code` in JSON body')

    response = await oauth_request(
        session := request.app['session'],
        DiscordRoute('POST', '/oauth2/token'),
        grant_type='authorization_code', code=code, redirect_uri=website
    )
    if 'access_token' not in response:
        raise web.HTTPServerError(text='Response did not contain access_token')

    return await update_access_token(session, data=response)


@routes.patch('/oauth')
@limiter.limit('5/5second')
async def oauth_refresh_token(request: web.Request) -> web.Response:
    data = await request.json()
    refresh_token = data.get('refresh_token')
    if not refresh_token:
        raise web.HTTPBadRequest(text='Must have `refresh_token` in JSON body')

    response = await oauth_request(
        session := request.app['session'],
        DiscordRoute('POST', '/oauth2/token'),
        grant_type='refresh_token', refresh_token=refresh_token,
    )
    if 'access_token' not in response:
        raise web.HTTPServerError(text='Response did not contain access_token')

    return await update_access_token(session, data=response)


@routes.delete('/oauth')
@limiter.limit('5/5second')
async def oauth_revoke_token(request: web.Request) -> web.Response:
    token = request.query.get('token')
    if not token:
        raise web.HTTPBadRequest(text='Must have `token` in query parameters')

    await oauth_request(
        request.app['session'],
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

    session = request.app['session']
    response = await authenticated_discord_request(session, DiscordRoute('GET', '/users/@me'), token=token)
    return web.json_response(response)


@routes.post('/checkout/subscription')
@limiter.limit('3/15second')
async def checkout_subscription(request: web.Request):
    data = request.query
    recipient_id = data.get('recipient_id')  # User ID or guild ID

    custom_field = {
        'key': 'recipient_id',
        'label': {
            'custom': 'User ID of Recipient (if this is a gift)',
            'type': 'custom',
        },
        'type': 'text',
        'optional': True,
        'text': {
            'minimum_length': 17,
            'maximum_length': 22,
        },
    }
    if recipient_id and 17 <= len(recipient_id) <= 22:
        try:
            int(recipient_id)
        except ValueError:
            raise web.HTTPBadRequest(text='Invalid recipient ID format')
        custom_field['text']['default_value'] = recipient_id

    quantity = data.get('quantity', 1)
    if not isinstance(quantity, int) or quantity < 1:
        raise web.HTTPBadRequest(text='Quantity must be a positive integer')
    if quantity > 100:
        raise web.HTTPBadRequest(text='Quantity cannot exceed 100')

    product = data.get('product')
    if product not in ('coined_silver', 'coined_gold', 'coined_premium'):
        raise web.HTTPBadRequest(text='Invalid product specified')

    if product in ('coined_silver', 'coined_gold') and quantity != 1:
        raise web.HTTPBadRequest(text='Silver and Gold subscriptions can only be purchased in quantity of 1')
    if product == 'coined_premium':
        custom_field['label']['custom'] = 'Server ID to apply Coined Premium to'

    sku = getattr(StripeSKUs, product)
    stripe: StripeClient = request.app['stripe']
    kwargs = dict(
        line_items=[{'price': sku, 'quantity': quantity}],
        mode='subscription',
        success_url=f'{website}/checkout-success',
        cancel_url=f'{website}/store',
        custom_fields=[custom_field],
        allow_promotion_codes=True,
    )
    if email := data.get('email'):
        kwargs['customer_email'] = email
    if coupon := data.get('coupon'):
        kwargs['discounts'] = [{'coupon': coupon}]
        kwargs['payment_method_collection'] = 'if_required'
        kwargs.pop('allow_promotion_codes', None)

    session = await stripe.checkout.sessions.create_async(kwargs)
    raise web.HTTPSeeOther(session.url)


async def create_app() -> web.Application:
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

    app['session'] = ClientSession()
    app.on_cleanup.append(lambda a: a['session'].close())
    app['stripe'] = StripeClient(stripe_api_key, http_client=StripeAiohttp())
    return app


if __name__ == '__main__':
    web.run_app(create_app(), port=8090)
