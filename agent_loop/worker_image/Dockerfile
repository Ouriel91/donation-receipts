FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
# Preparation only: never build this image with candidate code or repository context.
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple pytest==9.0.3 iniconfig==2.3.0 packaging==26.0 pluggy==1.6.0 pygments==2.19.2 && python -m pip uninstall -y pip
COPY run.py /opt/worker.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HOME=/tmp
USER 65534:65534
WORKDIR /input
ENTRYPOINT ["python", "-I", "/opt/worker.py"]
