import dotenv
import typer


from cli import backtest
from cli import catalog
from cli import live


dotenv.load_dotenv()


cli = typer.Typer(pretty_exceptions_enable=False)


cli.add_typer(backtest.cli, name="backtest")
cli.add_typer(catalog.cli, name="catalog")
cli.add_typer(live.cli, name="live")


if __name__ == "__main__":
    cli()
