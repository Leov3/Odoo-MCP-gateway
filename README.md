# Odoo MCP Gateway

MVP funcional en FastAPI para administrar múltiples instancias de Odoo desde un panel simple y exponer tools MCP desde un único servicio.

## Requisitos

- Python 3.12
- Docker y Docker Compose

## Generar `ENCRYPTION_KEY`

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Configuración local

1. Copia `.env.example` a `.env`.
2. Completa estas variables:
   - `ADMIN_USER`
   - `ADMIN_PASSWORD`
   - `MCP_BEARER_TOKEN`
   - `ENCRYPTION_KEY`
   - `APP_SECRET_KEY`

## Ejecutar con Docker

```bash
docker compose up --build
```

La aplicación queda disponible en:

- Panel admin: `http://localhost:8000/admin`
- Login: `http://localhost:8000/admin/login`
- Ajustes de usuario: `http://localhost:8000/admin/settings`
- Health check: `http://localhost:8000/health`
- MCP: `http://localhost:8000/mcp`

## Entrar al panel

Usa el usuario y contraseña definidos en:

- `ADMIN_USER`
- `ADMIN_PASSWORD`

Desde `Settings` puedes cambiar el nombre de usuario y la contraseña del único admin.

## Configurar Easypanel

1. Crea un nuevo servicio desde este repositorio.
2. Usa el `Dockerfile` incluido.
3. Expón el puerto `8000`.
4. Agrega las variables de entorno del `.env`.
5. Monta el volumen persistente en `/data`.
6. Despliega el servicio.

La base SQLite se guarda en `/data/app.db`.

## Conectar un cliente MCP

Usa el endpoint:

```text
https://tu-dominio/mcp
```

El endpoint requiere este encabezado:

```text
Authorization: Bearer <MCP_BEARER_TOKEN>
```

Ejemplo:

```bash
curl -H "Authorization: Bearer change_me_long_random_token" https://tu-dominio/mcp
```

## Seguridad

- No compartas el `MCP_BEARER_TOKEN`.
- No subas `.env` al repositorio.
- El secreto de cada instancia se guarda cifrado con Fernet.
- En edición, el campo secreto se deja vacío y solo se actualiza si escribes uno nuevo.
- Este proyecto no incluye multiusuario, OAuth, ni permisos avanzados.
