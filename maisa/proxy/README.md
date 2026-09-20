# albertitos-proxy — HTTPS del sistema Albertitos

Proxy inverso **Caddy** que termina TLS y reenvía a la API/BFF. Es lo que permite
que el visor (Framer/Vercel, servido por **HTTPS**) consuma la API: un navegador
**no deja** que una página `https://` llame a una API `http://` — bloquea la
petición por *mixed content* antes de que salga, y en la consola del navegador
solo se ve `blocked: mixed-content`. Este contenedor es la pieza que quita ese
bloqueo.

```
navegador (https)
      │  TLS 1.3
      ▼
82.70.78.22.sslip.io:443        albertitos-proxy (Caddy)
      │  http, red interna albertitos_net
      ▼
albertitos-api:8000             albertitos-api (FastAPI)
```

- **URL pública:** `https://82.70.78.22.sslip.io`
- **Certificado:** Let's Encrypt, emitido y renovado solo por Caddy.
- **No sustituye al 8010:** `http://82.70.78.22:8010` sigue funcionando tal cual
  (diagnóstico y smoke test). Solo se **añade** la vía HTTPS.

---

## 1. Por qué `sslip.io` y no un dominio

Un certificado de Let's Encrypt no se puede emitir para una **IP pelada**: la CA
solo valida nombres. Este despliegue no tiene dominio, así que se usa
[`sslip.io`](https://sslip.io), un DNS comodín que resuelve a la IP que va
escrita en el propio nombre:

```console
$ getent hosts 82.70.78.22.sslip.io
82.70.78.22     82.70.78.22.sslip.io
```

Para Let's Encrypt `82.70.78.22.sslip.io` es un nombre normal: el reto HTTP-01 se
sirve desde esta máquina y la CA lo valida desde fuera. **No hay que comprar ni
registrar nada.** `sslip.io` está en la *Public Suffix List*, así que el límite
de emisiones de Let's Encrypt se aplica a este nombre y no lo comparten otros
usuarios.

Si algún día hay dominio propio, basta con cambiar `SITE_ADDRESS` (ver §5): Caddy
pide el certificado del nombre nuevo sin tocar nada más.

---

## 2. Arranque

Requisitos, en este orden:

```bash
# 1. La red compartida (una sola vez; normalmente ya existe)
docker network create albertitos_net

# 2. La API tiene que estar arriba: Caddy solo la reenvia
docker compose -f maisa/api/docker-compose.yml up -d

# 3. El proxy
docker compose -f maisa/proxy/docker-compose.yml up -d
docker compose -f maisa/proxy/docker-compose.yml logs -f caddy
```

En el log tiene que aparecer, una sola vez:

```
"msg":"certificate obtained successfully","identifier":"82.70.78.22.sslip.io",
  "issuer":"acme-v02.api.letsencrypt.org-directory"
```

Y a partir de ahí, en cada petición, una línea `handled request`.

### Requisito de infraestructura: el NSG tiene que dejar pasar 80 y 443

Sin esto el reto ACME **no llega** y Caddy reintenta en bucle (con espera
creciente) sin emitir certificado. El NSG de la instancia está en OCI:

```bash
NSG=ocid1.networksecuritygroup.oc1.eu-madrid-3.aaaaaaaavpkg26br3rmhiov6ai4nay4mjqdu2zzrnd4r5qzmi5jropgkye3a

oci network nsg rules add --nsg-id "$NSG" --security-rules '[{
  "direction":"INGRESS","protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK",
  "isStateless":false,"tcpOptions":{"destinationPortRange":{"min":80,"max":80}}
},{
  "direction":"INGRESS","protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK",
  "isStateless":false,"tcpOptions":{"destinationPortRange":{"min":443,"max":443}}
}]'
```

Comprobar cómo quedó:

```bash
oci network nsg rules list --nsg-id "$NSG" --all \
  --query 'data[].{dir:direction,port:"tcp-options"."destination-port-range".min,src:source}' \
  --output table
```

> **Hay dos NSG con el mismo nombre** (`ig-quick-action-NSG`, resto de una *quick
> action* de la consola) y los dos cuelgan de la misma VNIC. La regla del `8010`
> vive en el primero, y es ahí donde se añaden las del 80 y el 443. Como OCI solo
> tiene reglas de permiso, tener las dos es inocuo. El segundo NSG solo tiene
> `EGRESS`: si se aplican las reglas ahí, no pasa nada, pero conviene no
> dispersarlas.

Para **cerrar** los puertos otra vez, se quitan esas dos reglas con
`oci network nsg rules remove` (mismo formato).

---

## 3. Qué hace exactamente

`Caddyfile` es corto a propósito; esto es todo lo que hace Caddy:

| Pieza | Qué hace |
|---|---|
| `acme_ca .../letsencrypt` | Fija la CA. Sin esto Caddy intenta también ZeroSSL, que exige credenciales EAB y mete un error por cada emisión. |
| `email` (comentado) | Opcional. Un correo de relleno **no** vale: Let's Encrypt rechaza `example.com` con `invalidContact` y **no emite** el certificado. Ver §5. |
| `SITE_ADDRESS` | Pide el certificado (HTTP-01 por el 80, TLS-ALPN por el 443) y lo renueva solo. |
| `encode zstd gzip` | Comprime el visor y los JSON. |
| `reverse_proxy albertitos-api:8000` | Reenvía a la API por el DNS interno de Docker. **Puerto 8000**, el interno: el 8010 es solo lo que se publica en el anfitrión. |
| `header_up X-Real-IP` | El cliente real, para los logs de la API. |
| `log` | Una línea por petición en el log del contenedor. |
| `header Strict-Transport-Security` | Marca el nombre como solo-HTTPS. |

El tráfico **navegador → Caddy** va cifrado. **Caddy → API** va en claro por la
red `albertitos_net`, que no sale de la máquina: no hay nada que cifrar ahí.

### Cabeceras de proxy: `FORWARDED_ALLOW_IPS`

Caddy añade `X-Forwarded-Proto: https`. Pero uvicorn **solo se cree** esa
cabecera si la IP de origen está en `FORWARDED_ALLOW_IPS` (por defecto solo
`127.0.0.1`). Detrás de un proxy la petición llega de otro contenedor, así que
sin configurarlo uvicorn cree que la petición entró por `http`: cualquier URL
absoluta o redirección que generase la API saldría en `http` y el navegador la
bloquearía igual, ahora por venir de una página `https`.

`maisa/api/docker-compose.yml` lo trae puesto a la **subred de `albertitos_net`**
(`172.20.0.0/16`), no a `*`: así un cliente que entre directo por el 8010 no
puede falsificar `X-Forwarded-For`. Se nota en el log de la API — con esto puesto
sale la IP real:

```
INFO:     82.70.78.22:0 - "GET /health HTTP/1.1" 200 OK
```

Si la red se recrea con otro rango, hay que ajustar el valor:

```bash
docker network inspect albertitos_net --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
```

---

## 4. Verificación

```bash
# Certificado válido y emitido por Let's Encrypt (tls=0 es "verificado")
curl -sS -o /dev/null -w 'status=%{http_code} tls=%{ssl_verify_result}\n' \
  https://82.70.78.22.sslip.io/health

echo | openssl s_client -connect 82.70.78.22.sslip.io:443 \
  -servername 82.70.78.22.sslip.io 2>/dev/null | openssl x509 -noout -subject -issuer -dates

# El http redirige al https (308), no sirve datos en claro
curl -sS -o /dev/null -w '%{http_code} -> %{redirect_url}\n' \
  http://82.70.78.22.sslip.io/api/meta

# El smoke test completo, ahora contra HTTPS
PUBLIC_IP=82.70.78.22 ./maisa/api/smoke_lan.sh --base https://82.70.78.22.sslip.io
```

> El smoke test usa `curl`, que **sí** verifica el certificado. Si el nombre no
> resolviera o el certificado fuera de otro nombre, el script falla: es la señal
> de que el HTTPS no está bien, no un falso positivo.

---

## 5. Cambiar el nombre o el correo

Copia la plantilla y edita:

```bash
cd maisa/proxy && cp .env.example .env
```

| Variable | Para qué |
|---|---|
| `SITE_ADDRESS` | El nombre público. Con dominio propio: el dominio, y su registro `A` apuntando a `82.70.78.22`. |
| `API_UPSTREAM` | Destino. Por defecto `albertitos-api:8000`. |

Después, `docker compose -f maisa/proxy/docker-compose.yml up -d`.

El **correo de contacto** no es variable de entorno: es la opción global `email`
del `Caddyfile`, que viene **comentada**. Para activarlo, descomenta la línea con
un correo **real** y `docker compose restart caddy`. Sirve para recibir el aviso
si la renovación automática empieza a fallar; sin él el certificado se emite y
se renueva igual.

> **Límite de Let's Encrypt:** 5 emisiones por nombre y semana. No borres el
> volumen `caddy_data` "para probar": ahí viven el certificado y la cuenta ACME,
> y cada recreación desde cero gasta una emisión.

---

## 6. Problemas conocidos

| Síntoma | Causa real |
|---|---|
| Log con `invalidContact ... forbidden domain "example.com"` y sin certificado | Alguien puso un correo de relleno en `email`. Let's Encrypt no lo acepta: descomenta la línea con un correo real o déjala comentada. |
| `no se pudo validar el nombre` / Caddy reintenta en bucle | El 80 o el 443 no están abiertos en el NSG (§2). El reto ACME tiene que llegar desde Internet. |
| `502` en el HTTPS | La API no está arriba, o `API_UPSTREAM` no apunta al DNS correcto de `albertitos_net`. `docker compose -f maisa/api/docker-compose.yml ps`. |
| La API responde, pero el navegador sigue bloqueando | El visor llama a `http://82.70.78.22:8010` en lugar de a `https://82.70.78.22.sslip.io`. La URL del frontend hay que cambiarla también. |
| El navegador avisa de CORS | El origen del visor no está en `CORS_ORIGINS`. `maisa/api/.env` lo tiene hoy en `*`; para cerrarlo, ver `maisa/api/README.md` §4. |
| `certificate obtained successfully` pero `curl` da error de certificado | Se está llamando por la IP (`https://82.70.78.22/`): el certificado es para **el nombre**. Hay que usar el nombre. |
