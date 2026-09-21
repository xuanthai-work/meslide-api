async def bootstrap_database_user() -> None:
    """Startup hook retained for lifespan ordering.

    Account rows are created by ``seed_local_users`` or the parent Gapo app.
    This no longer auto-creates a demo user.
    """
    return
