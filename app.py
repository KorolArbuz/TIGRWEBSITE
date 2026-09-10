from shop import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    # Direct launch is loopback-only; local scripts explicitly select development.
    uvicorn.run("app:app", host="127.0.0.1", port=8000, proxy_headers=False, access_log=False)
