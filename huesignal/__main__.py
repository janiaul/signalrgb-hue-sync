"""Entry point — allows running the package with `python -m huesignal`."""

from .app import HueSignalApp


def main() -> None:
    HueSignalApp().run()


if __name__ == "__main__":
    main()
