FROM alpine:3.16.2

RUN apk add --no-cache \
  py3-click \
  py3-pygithub \
  py3-pynacl \
  py3-yaml

WORKDIR /usr/src/app

#COPY requirements.txt ./
#RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python3", "./cli.py"]
