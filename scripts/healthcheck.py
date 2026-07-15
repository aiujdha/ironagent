from urllib.request import urlopen


def main() -> None:
    with urlopen("http://localhost:8000/health") as response:  # noqa: S310
        print(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
