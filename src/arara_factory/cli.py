from pathlib import Path

import typer
import yaml

app = typer.Typer(no_args_is_help=True)


@app.command()
def generate(
    input_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    config: Path = typer.Option(Path("config/pipeline.yaml"), exists=True),
) -> None:
    """Validate inputs and prepare an ARARA render job."""
    settings = yaml.safe_load(config.read_text(encoding="utf-8"))
    typer.echo(f"Verticliv: {input_file.name}")
    typer.echo(f"Variants: {settings.get('variants', 1)}")
    typer.echo("Pipeline scaffold is ready; FFmpeg renderer is the next module.")


if __name__ == "__main__":
    app()
