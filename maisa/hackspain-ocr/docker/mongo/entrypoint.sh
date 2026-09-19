#!/bin/sh
# ---------------------------------------------------------------------------
# Entrypoint propio del contenedor de MongoDB.
#
# POR QUE EXISTE ESTE FICHERO
#
# Un replica set, aunque sea de un unico nodo, exige autenticacion INTERNA entre
# miembros. Si ademas se activa la autorizacion de clientes (lo que hace
# "security.keyFile" de forma implicita), mongod aborta al arrancar con:
#
#   BadValue: security.keyFile is required when authorization is enabled with
#             replica sets
#
# La imagen oficial mongo:7.0 NO genera ese keyfile, asi que lo generamos aqui
# antes de ceder el control al entrypoint oficial.
#
# El keyfile se guarda en /data/configdb (volumen propio, separado del dbPath
# /data/db), por lo que sobrevive a los reinicios del contenedor y a
# "docker compose down". En un replica set de varios nodos TODOS los miembros
# tendrian que compartir exactamente el mismo keyfile.
#
# Referencia: https://www.mongodb.com/docs/manual/tutorial/deploy-replica-set-with-keyfile-access-control/
# ---------------------------------------------------------------------------
set -eu

KEYFILE="${MONGO_KEYFILE_PATH:-/data/configdb/keyfile}"

if [ ! -s "$KEYFILE" ]; then
	echo "[mongo-entrypoint] Generando keyfile de autenticacion interna en ${KEYFILE}"
	mkdir -p "$(dirname "$KEYFILE")"
	# MongoDB exige un keyfile de entre 6 y 1024 caracteres en base64 y permisos
	# de lectura unicamente para el propietario (400).
	openssl rand -base64 756 >"$KEYFILE"
	chmod 400 "$KEYFILE"
	chown mongodb:mongodb "$KEYFILE"
else
	echo "[mongo-entrypoint] Reutilizando keyfile existente en ${KEYFILE}"
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
