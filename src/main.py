from fastapi import FastAPI

from src.security_service.app import create_security_app


def create_app() -> FastAPI:
    return create_security_app()


app = create_app()
