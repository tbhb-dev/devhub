#!/bin/bash
# cspell:ignore newkey keyout CAcreateserial extfile addext
set -euo pipefail
mkdir -p /run/temporal-fixtures
cd /run/temporal-fixtures
for ca in ca unrelated; do
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -keyout "$ca.key" \
    -out "$ca.pem" -subj /CN=example.com \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign
done
printf 'subjectAltName=DNS:temporal-server,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n' >extensions
for leaf in server client wrong; do
  ca=ca
  if [[ $leaf == wrong ]]; then ca=unrelated; fi
  openssl req -newkey rsa:2048 -nodes -keyout "$leaf.key" -out "$leaf.csr" -subj /CN=temporal-server
  openssl x509 -req -days 2 -in "$leaf.csr" -CA "$ca.pem" -CAkey "$ca.key" \
    -CAcreateserial -out "$leaf.pem" -extfile extensions
done
chmod 0644 ./*.key
