from sanic import request

async def auth(request: request.Request):
    # Write you code here to return initial launch url
    company_id = int(request.args.get("company_id"))
    return f"{request.conn_info.ctx.extension.base_url}/company/{company_id}"


async def uninstall(request):
    # Write your code here to cleanup data related to extension
    # If task is time taking then process it async on other process
    pass


extension_handler = {
    "auth": auth,
    "uninstall": uninstall
}
