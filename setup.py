from setuptools import find_packages, setup

with open("README.md", "r") as f:
    long_description = f.read()

setup(
    name="gqljit",
    description="A GraphQL query JIT compiler",
    url="https://github.com/p7g/gqljit",
    author="Patrick Gingras <775.pg.12@gmail.com>",
    author_email="775.pg.12@gmail.com",
    classifiers=[
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
    ],
    keywords="graphql, jit",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=find_packages(where="gqljit"),
    package_data={"gqljit": ["py.typed"]},
    python_requires=">=3.8, <4",
    install_requires=[
        "llvmlite>=0.36.0rc1",
        "graphql-core>=3.1.3",
    ],
    setup_requires=["setuptools_scm"],
    tests_require=["riot"],
    use_scm_version=True,
    zip_safe=False,
)
