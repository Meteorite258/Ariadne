FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
# Standard library only: the image has no runtime package installation or network requirement.
COPY runner.py /runner.py
COPY evidence.py /evidence.py
COPY execute.py /execute.py
USER 65534:65534
WORKDIR /outputs
ENTRYPOINT ["python", "-I", "/runner.py"]
