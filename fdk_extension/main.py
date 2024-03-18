"""Setup fdk file."""
from fdk_client.application.ApplicationClient import ApplicationClient
from fdk_client.application.ApplicationConfig import ApplicationConfig
from fdk_client.platform.PlatformClient import PlatformClient
from fdk_client.partner import PartnerClient

from .api_blueprints import setup_proxy_routes
from .extension import FdkExtensionClient
from .extension import extension
from .handlers import setup_routes
from .session.session import Session
from .session.session_storage import SessionStorage

import asyncio


async def get_platform_client(company_id: str) -> PlatformClient:
    client = None
    if not extension.is_online_access_mode():
        sid = Session.generate_session_id(False, **{
            "cluster": extension.cluster,
            "id": company_id
        })
        session = await SessionStorage.get_session(sid)
        client = await extension.get_platform_client(company_id, session)

    return client

async def get_partner_client(organization_id: str) -> PartnerClient:
    client = None
    if not extension.is_online_access_mode():
        sid = Session.generate_session_id(False, **{
            "cluster": extension.cluster,
            "id": organization_id
        })
        session = await SessionStorage.get_session(sid)
        client = await extension.get_partner_client(organization_id, session)

    return client

async def get_application_client(application_id: str, application_token: str) -> ApplicationClient:
    application_config = ApplicationConfig({
        "applicationID": application_id,
        "applicationToken": application_token,
        "domain": extension.cluster
    })
    application_client = ApplicationClient(application_config)
    return application_client


def setup_fdk(data: dict) -> FdkExtensionClient:
    asyncio.run(extension.initialize(data))

    fdk_route = setup_routes()
    platform_api_routes, application_proxy_routes, partner_proxy_routes = setup_proxy_routes()

    return FdkExtensionClient(**{
        "fdk_handler": fdk_route,
        "extension": extension,
        "platform_api_routes": platform_api_routes,
        "webhook_registry": extension.webhook_registry,
        "application_proxy_routes": application_proxy_routes,
        "partner_proxy_routes": partner_proxy_routes,
        "get_platform_client": get_platform_client,
        "get_partner_client": get_partner_client,
        "get_application_client": get_application_client
    })