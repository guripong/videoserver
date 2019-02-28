#!/usr/bin/env bash

KEYDIR=/opt/awscam/awsmedia/certs

# Set the wildcarded domain
# we want to use
DOMAIN="localhost"

# A blank passphrase
PASSPHRASE="DeepLens"

# Information needed for root key
SUBJ="
C=US
ST=WA
O=AWS DeepLens
commonName=Video Server Certificate Authority
organizationalUnitName=
emailAddress=
"

# Generate the root key
openssl genrsa -out "$KEYDIR"/root.key 2048

# And a self-signed certificate
openssl req -x509 -new -nodes -key "$KEYDIR"/root.key -days 3650 -out "$KEYDIR"/root.crt -subj "$(echo -n "$SUBJ" | tr "\n" "/")" -passin pass:$PASSPHRASE

# Generate server and client keys
openssl genrsa -out "$KEYDIR"/server.key 2048
openssl genrsa -out "$KEYDIR"/client.key 2048

# Information needed for server certificate
SERVER_SUBJ="
C=US
ST=WA
O=AWS DeepLens
commonName=$DOMAIN
organizationalUnitName=
emailAddress=
"

# Information needed for client certificate
CLIENT_SUBJ="
C=US
ST=WA
O=AWS DeepLens
commonName=Client Certs
organizationalUnitName=
emailAddress=
"

# Create certificate signing requests
openssl req -new -nodes -key "$KEYDIR"/server.key -days 3650 -out "$KEYDIR"/server.csr -subj "$(echo -n "$SERVER_SUBJ" | tr "\n" "/")" -passin pass:$PASSPHRASE
openssl req -new -nodes -key "$KEYDIR"/client.key -days 3650 -out "$KEYDIR"/client.csr -subj "$(echo -n "$CLIENT_SUBJ" | tr "\n" "/")" -passin pass:$PASSPHRASE

for name in server client; do 
    openssl x509 -req -in "$KEYDIR/$name".csr -CA "$KEYDIR"/root.crt -CAkey "$KEYDIR"/root.key -CAcreateserial -out "$KEYDIR/$name".crt -days 3650
done

rm "$KEYDIR"/root.key
rm "$KEYDIR"/server.csr

# Validate
for name in server client; do
    openssl verify -CAfile "$KEYDIR"/root.crt "$KEYDIR/$name".crt
done

echo "exporting client certificates in pkcs12"
openssl pkcs12 -export -in "$KEYDIR/"client.crt -inkey "$KEYDIR/"client.key -out "$KEYDIR"/client.pfx -passout pass:$PASSPHRASE

rm "$KEYDIR"/client.key
rm "$KEYDIR"/client.csr
rm "$KEYDIR"/client.crt
rm "$KEYDIR"/root.srl
