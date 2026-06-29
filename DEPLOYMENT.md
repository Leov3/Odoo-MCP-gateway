# Guia de despliegue

Esta guia cubre despliegue local con Docker y despliegue en Easypanel.

## 1. Variables de entorno

Crea un archivo `.env` a partir de `.env.example` y define:

- `ADMIN_USER`
- `ADMIN_PASSWORD`
- `MCP_BEARER_TOKEN`
- `ENCRYPTION_KEY`
- `APP_SECRET_KEY`

Genera `ENCRYPTION_KEY` con:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 2. Ejecutar localmente

```bash
docker compose up --build
```

Verifica:

- `http://localhost:8000/health`
- `http://localhost:8000/admin/login`
- `http://localhost:8000/admin/instances`

Credenciales iniciales:

- Usuario: `ADMIN_USER`
- Password: `ADMIN_PASSWORD`

## 3. Despliegue en Easypanel

### Crear el servicio

1. En Easypanel crea un nuevo proyecto o servicio desde GitHub.
2. Conecta este repo: `Leov3/Odoo-MCP-gateway`.
3. Usa el `Dockerfile` del repositorio.
4. Expón el puerto `8000`.
5. Agrega las variables de entorno del `.env`.

### Persistencia

Monta un volumen persistente en:

```text
/data
```

La base SQLite se guarda en:

```text
/data/app.db
```

### Arranque

No necesitas comandos extra. El contenedor arranca con:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

El `Dockerfile` ya habilita `--proxy-headers` para que `request.base_url` refleje el dominio público cuando el servicio está detrás de un proxy inverso.

## 4. Verificaciones

Después del deploy confirma:

- `GET /health` responde `{"status":"ok"}`
- `GET /admin/login` carga el panel
- `GET /admin/settings` permite cambiar usuario y contraseña
- `GET /mcp` responde un error deprecado
- `GET /compraloahora/mcp/` responde `401` sin token
- `POST /compraloahora/mcp/` con bearer válido responde como endpoint MCP

## 5. Conectar un cliente MCP

Usa el endpoint de la instancia:

```text
https://tu-dominio/compraloahora/mcp/
```

Incluye este header:

```text
Authorization: Bearer <MCP_BEARER_TOKEN>
```

Nota:

- usa la barra final `/mcp/`
- la instancia va en la ruta, no en un query string
- si el cliente no soporta `Bearer Auth`, fuerza el header manualmente como `Authorization: Bearer <MCP_BEARER_TOKEN>`

Para n8n:

- `Server Transport`: `HTTP Streamable`
- `MCP Endpoint URL`: `https://tu-dominio/compraloahora/mcp/`
- `Authentication`: `Bearer Auth` o `Header Auth`
- Con `Bearer Auth`, pega solo el token.
- Con `Header Auth`, define exactamente `Authorization: Bearer <MCP_BEARER_TOKEN>`

### Validado con n8n

La instalación probada funcionó con:

- `MCP Endpoint URL`: `https://dev-odoo-mcp-gateway.ouiteb.easypanel.host/compraloahora/mcp/`
- `Authorization`: `Bearer <MCP_BEARER_TOKEN>`
- Instancia usada en las tools: `Compraloahora`
- `odoo_test_connection`: OK

Si el cliente da problemas con `Bearer Auth`, usa `Header Auth` y fuerza este header:

```text
Authorization: Bearer <MCP_BEARER_TOKEN>
```

## 6. Notas operativas

- No subas `.env` al repositorio.
- No borres el volumen `/data` si quieres conservar las instancias.
- El panel usa una sola cuenta admin.
- El secreto de cada instancia se guarda cifrado.
- El endpoint compartido `/mcp` quedó como legado y ya no es el recomendado.
- El flujo de Mario debe llevar su memoria, carrito y cotización pendiente en n8n; el MCP solo expone herramientas de negocio.
