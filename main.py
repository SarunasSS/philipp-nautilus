import dotenv
import typer


from cli import backtest


dotenv.load_dotenv()


cli = typer.Typer(pretty_exceptions_enable=False)


cli.add_typer(backtest.cli, name="backtest")


if __name__ == "__main__":
    cli()
