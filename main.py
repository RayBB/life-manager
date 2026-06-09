import typer

app = typer.Typer()


@app.command()
def main():
    print("Hello from life-manager!")


if __name__ == "__main__":
    app()
