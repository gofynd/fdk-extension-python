"""Request handlers."""
from datetime import datetime, timedelta
from urllib.parse import urljoin
import uuid

from sanic.blueprints import Blueprint
from sanic.blueprint_group import BlueprintGroup
from sanic.response import json as json_response
from sanic.response import redirect
from sanic.request import Request

from .constants import *
from .exceptions import FdkSessionNotFoundError, FdkInvalidOAuthError
from .extension import extension
from .middleware.session_middleware import session_middleware, partner_session_middleware
from .session.session import Session
from .session.session_storage import SessionStorage
from .utilities import logger
from .utilities.utility import get_company_cookie_name

logger = logger.get_logger()


async def install_handler(request: Request):
    try:
        company_id = int(request.args.get("company_id"))
        platform_config = extension.get_platform_config(company_id)

        session = Session(Session.generate_session_id(True))
        session_expires = datetime.now() + timedelta(seconds=SESSION_EXPIRY_IN_SECONDS) # 15 mins

        if session.is_new:
            session.company_id = company_id
            session.scope = extension.scopes
            session.expires = session_expires
            session.access_mode = ONLINE_ACCESS_MODE  # Always generate online mode token for extension launch
            session.extension_id = extension.api_key

        request.conn_info.ctx.fdk_session = session
        request.conn_info.ctx.extension = extension

        session.state = str(uuid.uuid4())

        auth_callback = extension.get_auth_callback()

        # pass application id if received
        if request.args.get("application_id"):
            auth_callback += "?application_id=" + request.args.get("application_id")

        # start authorization flow
        redirect_url = platform_config.oauthClient.startAuthorization({
            "scope": session.scope,
            "redirectUri": auth_callback,
            "state": session.state,
            "access_mode": ONLINE_ACCESS_MODE # Always generate online mode token for extension launch
        })

        logger.debug(f"Redirecting after install callback to url: {redirect_url}")

        company_cookie_name =  get_company_cookie_name(company_id=company_id)

        next_response = redirect(redirect_url, headers={"x-company-id": str(company_id)})
        next_response.cookies[company_cookie_name] = session.session_id
        next_response.cookies[company_cookie_name]["secure"] = True
        next_response.cookies[company_cookie_name]["samesite"] = "None"
        next_response.cookies[company_cookie_name]["httponly"] = True
        next_response.cookies[company_cookie_name]["expires"] = session.expires

        await SessionStorage.save_session(session)

        return next_response
    except Exception as e:
        logger.exception(e)
        return json_response({"error_message": str(e)}, 500)


async def auth_handler(request: Request):
    try:
        if not request.conn_info.ctx.fdk_session:
            raise FdkSessionNotFoundError("Can not complete oauth process as session not found")

        if request.conn_info.ctx.fdk_session.state != request.args.get("state"):
            raise FdkInvalidOAuthError("Invalid oauth call")

        company_id = request.conn_info.ctx.fdk_session.company_id

        platform_config = extension.get_platform_config(company_id)
        await platform_config.oauthClient.verifyCallback(request.args)

        token: dict = platform_config.oauthClient.raw_token
        session_expires = datetime.now() + timedelta(seconds=token["expires_in"])

        request.conn_info.ctx.fdk_session.expires = session_expires
        token["access_token_validity"] = int(session_expires.timestamp()*1000)
        request.conn_info.ctx.fdk_session.update_token(token)

        await SessionStorage.save_session(request.conn_info.ctx.fdk_session)


        if not extension.is_online_access_mode():
            session_id = Session.generate_session_id(False, **{
                "cluster": extension.cluster,
                "id": company_id
            })
            session = await SessionStorage.get_session(session_id)
            if not session:
                session = Session(session_id=session_id)
            elif session.extension_id != extension.api_key:
                session = Session(session_id=session_id)
            
            platform_config = extension.get_platform_config(company_id)
            offline_token_response = await platform_config.oauthClient.getOfflineAccessToken(
                extension.scopes, request.args.get("code")
                )
            
            session.company_id = company_id
            session.scope = extension.scopes
            session.state = request.conn_info.ctx.fdk_session.state
            session.extension_id = extension.api_key
            offline_token_response["access_token_validity"] = platform_config.oauthClient.token_expires_at
            offline_token_response["access_mode"] = OFFLINE_ACCESS_MODE
            session.update_token(offline_token_response)

            await SessionStorage.save_session(session=session)

        request.conn_info.ctx.extension = extension

        if extension.webhook_registry.is_initialized:
            client = await extension.get_platform_client(
                company_id=company_id, session=request.conn_info.ctx.fdk_session)
            await extension.webhook_registry.sync_events(client, None, True)
        
        redirect_url = await extension.callbacks["auth"](request)
        next_response = redirect(redirect_url, headers={"x-company-id": str(company_id)})

        company_cookie_name = get_company_cookie_name(company_id=company_id)
        next_response.cookies[company_cookie_name] = request.conn_info.ctx.fdk_session.session_id
        next_response.cookies[company_cookie_name]["secure"] = True
        next_response.cookies[company_cookie_name]["samesite"] = "None"
        next_response.cookies[company_cookie_name]["httponly"] = True
        next_response.cookies[company_cookie_name]["expires"] = session_expires

        logger.debug(f"Redirecting after auth callback to url: {redirect_url}")

        return next_response
    except Exception as e:
        logger.exception(e)
        return json_response({"error_message": str(e)}, 500)


async def auto_install_handler(request: Request):
    try:
        company_id, code = int(request.json.get("company_id")), request.json.get("code")

        logger.debug(f"Extension auto install started for company: {company_id} on company creation.")

        platform_config = extension.get_platform_config(company_id=company_id)
        session_id = Session.generate_session_id(False, **{
            "cluster": extension.cluster,
            "id": company_id
        })

        session = await SessionStorage.get_session(session_id=session_id)
        if not session:
            session = Session(session_id=session_id)
        elif session.extension_id != extension.api_key:
            session = Session(session_id=session_id)

        offline_token_response = await platform_config.oauthClient.getOfflineAccessToken(extension.scopes, code=code)

        session.company_id = company_id
        session.scope = extension.scopes
        session.state = str(uuid.uuid4())
        session.extension_id = extension.api_key
        offline_token_response["access_token_validity"] = platform_config.oauthClient.token_expires_at
        offline_token_response["access_mode"] = OFFLINE_ACCESS_MODE
        session.update_token(offline_token_response)

        if not extension.is_online_access_mode():
            await SessionStorage.save_session(session=session)

        if extension.webhook_registry.is_initialized:
            client = await extension.get_platform_client(
                company_id=company_id, session=request.conn_info.ctx.fdk_session)
            await extension.webhook_registry.sync_events(client, None, True)


        logger.debug(f"Extension installed for company: {company_id} on company creation.")

        if extension.callbacks["auto_install"]:
            await extension.callbacks["auto_install"](request)

            
        return json_response({ "message": "success" })
        
    except Exception as e:
        logger.exception(str(e))
        return json_response({"error_message": str(e)}, 500)


async def uninstall_handler(request: Request):
    try:
        company_id = request.json["company_id"]
        if not extension.is_online_access_mode():
            session_id = Session.generate_session_id(False, **{
                "cluster": extension.cluster,
                "id": company_id
            })
            await SessionStorage.delete_session(session_id=session_id)

        request.conn_info.ctx.extension = extension
        await extension.callbacks["uninstall"](request)
        return json_response({"success": True})
    except Exception as e:
        logger.exception(e)
        return json_response({"error_message": str(e)}, 500)

async def adm_install_handler(request: Request):
    try:
        organization_id = request.args.get("organization_id")
        partner_config = extension.get_partner_config(organization_id)

        session = Session(Session.generate_session_id(True))
        session_expires = datetime.now() + timedelta(seconds=SESSION_EXPIRY_IN_SECONDS) # 15 mins

        if session.is_new:
            session.organization_id = organization_id
            session.scope = extension.scopes
            session.expires = session_expires
            session.access_mode = ONLINE_ACCESS_MODE  # Always generate online mode token for extension launch
            session.extension_id = extension.api_key

        request.conn_info.ctx.fdk_session = session
        request.conn_info.ctx.extension = extension

        session.state = str(uuid.uuid4())

        auth_callback = extension.get_adm_auth_callback()

        # start authorization flow
        redirect_url = partner_config.oauthClient.startAuthorization({
            "scope": session.scope,
            "redirectUri": auth_callback,
            "state": session.state,
            "access_mode": ONLINE_ACCESS_MODE # Always generate online mode token for extension launch
        })

        logger.debug(f"Redirecting after partner install callback to url: {redirect_url}")

        cookie_name =  ADMIN_SESSION_COOKIE_NAME

        next_response = redirect(redirect_url)
        next_response.cookies[cookie_name] = session.session_id
        next_response.cookies[cookie_name]["secure"] = True
        next_response.cookies[cookie_name]["samesite"] = "None"
        next_response.cookies[cookie_name]["httponly"] = True
        next_response.cookies[cookie_name]["expires"] = session.expires

        await SessionStorage.save_session(session)

        return next_response
    except Exception as e:
        logger.exception(e)
        return json_response({"error_message": str(e)}, 500)

async def adm_auth_handler(request: Request):
    try:
        if not request.conn_info.ctx.fdk_session:
            raise FdkSessionNotFoundError("Can not complete oauth process as session not found")

        if request.conn_info.ctx.fdk_session.state != request.args.get("state"):
            raise FdkInvalidOAuthError("Invalid oauth call")

        organization_id = request.conn_info.ctx.fdk_session.organization_id

        partner_config = extension.get_partner_config(organization_id)
        await partner_config.oauthClient.verifyCallback(request.args)

        token: dict = partner_config.oauthClient.raw_token
        session_expires = datetime.now() + timedelta(seconds=token["expires_in"])

        request.conn_info.ctx.fdk_session.expires = session_expires
        token["access_token_validity"] = int(session_expires.timestamp()*1000)
        request.conn_info.ctx.fdk_session.update_token(token)

        await SessionStorage.save_session(request.conn_info.ctx.fdk_session)


        if not extension.is_online_access_mode():
            session_id = Session.generate_session_id(False, **{
                "cluster": extension.cluster,
                "id": organization_id
            })
            session = await SessionStorage.get_session(session_id)
            if not session:
                session = Session(session_id=session_id)
            elif session.extension_id != extension.api_key:
                session = Session(session_id=session_id)
            
            #TODO: Do we need this here again
            partner_config = extension.get_partner_config(organization_id)
            offline_token_response = await partner_config.oauthClient.getOfflineAccessToken(
                extension.scopes, request.args.get("code")
                )
            
            session.organization_id = organization_id
            session.scope = extension.scopes
            session.state = request.conn_info.ctx.fdk_session.state
            session.extension_id = extension.api_key
            offline_token_response["access_token_validity"] = partner_config.oauthClient.token_expires_at
            offline_token_response["access_mode"] = OFFLINE_ACCESS_MODE
            session.update_token(offline_token_response)

            await SessionStorage.save_session(session=session)        

        
        redirect_url = urljoin(extension.base_url, '/admin')
        next_response = redirect(redirect_url)

        cookie_name = ADMIN_SESSION_COOKIE_NAME
        next_response.cookies[cookie_name] = request.conn_info.ctx.fdk_session.session_id
        next_response.cookies[cookie_name]["secure"] = True
        next_response.cookies[cookie_name]["samesite"] = "None"
        next_response.cookies[cookie_name]["httponly"] = True
        next_response.cookies[cookie_name]["expires"] = session_expires

        logger.debug(f"Redirecting after auth callback to url: {redirect_url}")

        return next_response
    except Exception as e:
        logger.exception(e)
        return json_response({"error_message": str(e)}, 500)

def setup_routes() -> BlueprintGroup:
    fdk_routes_bp1 = Blueprint("fdk_routes_bp1")
    fdk_routes_bp2 = Blueprint("fdk_routes_bp2")
    fdk_routes_bp3 = Blueprint("fdk_routes_bp3")

    fdk_routes_bp1.middleware(session_middleware, "request")
    fdk_routes_bp1.add_route(auth_handler, "/fp/auth", methods=["GET"])
    fdk_routes_bp1.add_route(auto_install_handler, "/fp/auto_install", methods=["POST"])

    fdk_routes_bp2.add_route(install_handler, "/fp/install", methods=["GET"])
    fdk_routes_bp2.add_route(adm_install_handler, "/adm/install", methods=["GET"])
    fdk_routes_bp2.add_route(uninstall_handler, "/fp/uninstall", methods=["POST"])

    fdk_routes_bp3.middleware(partner_session_middleware, "request")
    fdk_routes_bp3.add_route(adm_auth_handler, "/adm/auth", methods=["GET"])

    fdk_route = Blueprint.group(fdk_routes_bp1, fdk_routes_bp2, fdk_routes_bp3)
    return fdk_route
