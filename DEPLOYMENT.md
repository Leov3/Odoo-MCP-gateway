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

## 4. Verificaciones

Después del deploy confirma:

- `GET /health` responde `{"status":"ok"}`
- `GET /admin/login` carga el panel
- `GET /admin/settings` permite cambiar usuario y contraseña
- `GET /mcp` responde `401` sin token

## 5. Conectar un cliente MCP

Usa el endpoint:

```text
https://tu-dominio/mcp
```

Incluye este header:

```text
Authorization: Bearer <MCP_BEARER_TOKEN>
```

## 6. Notas operativas

- No subas `.env` al repositorio.
- No borres el volumen `/data` si quieres conservar las instancias.
- El panel usa una sola cuenta admin.
- El secreto de cada instancia se guarda cifrado.

