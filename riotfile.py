from riot import Venv, latest

venv = Venv(
    pys=3,
    venvs=[
        Venv(
            pys=[3.8, 3.9],
            name="test",
            command="pytest {cmdargs}",
            pkgs={"pytest": "==6.2.2"},
        ),
        Venv(
            name="mypy",
            command="mypy gqljit",
            pkgs={"mypy": "==0.812"},
        ),
        Venv(
            pkgs={"black": "==20.8b1"},
            venvs=[
                Venv(name="fmt", command=r"black --exclude '/\.riot/' ."),
                Venv(name="black", command="black --exclude '/\.riot/' {cmdargs}"),
            ],
        ),
        Venv(
            name="flake8",
            pkgs={"flake8": "==3.8.4"},
            command="flake8 .",
        ),
    ],
)
