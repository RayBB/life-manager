import typer

from settings import settings

app = typer.Typer()


@app.command()
def main():
    print(f"Loading Grist doc: {settings.grist_doc_id}")


if __name__ == "__main__":
    app()
