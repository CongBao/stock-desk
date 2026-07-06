from logging.config import fileConfig

from alembic import context
from alembic.config import Config

from stock_desk.storage.database import create_engine_for_url
from stock_desk.storage.metadata import Base


config: Config = context.config
target_metadata = Base.metadata

if config.config_file_name is not None and config.get_section("loggers") is not None:
    fileConfig(config.config_file_name)


def database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("Alembic requires sqlalchemy.url")
    return url


def run_migrations_offline() -> None:
    """Run migrations without creating a database connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using the application's configured engine policy."""
    engine = create_engine_for_url(database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
