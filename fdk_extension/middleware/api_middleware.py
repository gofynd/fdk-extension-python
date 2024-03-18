import json

from fdk_client.application.ApplicationClient import ApplicationClient
from fdk_client.application.ApplicationConfig import ApplicationConfig
from sanic.response import json as json_response
from sanic.request import Request

from ..extension import extension




async def application_proxy_on_request(request: Request) -> None:
    if request.headers.get("x-user-data"):
        request.conn_info.ctx.user = json.loads(request.headers["x-user-data"])
        request.conn_info.ctx.user.user_id = request.conn_info.ctx.user._id
    if request.headers.get("x-application-data"):
        request.conn_info.ctx.application = json.loads(request.headers["x-application-data"])
        request.conn_info.ctx.application_config = ApplicationConfig({
            "applicationID": request.conn_info.ctx.application._id,
            "applicationToken": request.conn_info.ctx.application.token,
            "domain": extension.cluster
        })
        request.conn_info.ctx.application_client = ApplicationClient(request.conn_info.ctx.application_config)


async def platform_api_on_request(request: Request) -> None:
    if not request.conn_info.ctx.fdk_session:
        return json_response({"message": "unauthorized"}, status=401)
    client = await extension.get_platform_client(request.conn_info.ctx.fdk_session.company_id,
                                                 request.conn_info.ctx.fdk_session)
    request.conn_info.ctx.platform_client = client
    request.conn_info.ctx.extension = extension

async def partner_api_on_request(request: Request) -> None:
    if not request.conn_info.ctx.fdk_session:
        return json_response({"message": "unauthorized"}, status=401)
    client = await extension.get_partner_client(request.conn_info.ctx.fdk_session.organization_id,
                                                 request.conn_info.ctx.fdk_session)
    request.conn_info.ctx.partner_client = client
    request.conn_info.ctx.extension = extension
