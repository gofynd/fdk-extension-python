import asyncio

async def retry_middleware(handler, *args, **kwargs):
    initial_delay = 30
    delay = initial_delay
    max_retries = float('inf')  # Retry forever
    allowed_codes = (502, 503, 504)

    attempt = 0
    while True:
        attempt += 1
        try:
            # Call the handler to execute the actual request
            response = await handler(*args, **kwargs)
            status_code = response.get('status_code')
            if status_code in allowed_codes:
                if attempt <= 3:
                    delay = initial_delay
                else:
                    delay = (attempt - 3) * 60  # Increase delay after initial retries
                print(f"RetryInterceptor: Retrying request after {delay} seconds. Attempt: {attempt} Code: {status_code}")
                await asyncio.sleep(delay)
            else:
                return response
        except Exception as e:
            raise Exception(e)

    raise Exception(f"Failed to request after {max_retries} retries")
