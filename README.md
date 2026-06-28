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
- MCP por instancia: `http://localhost:8000/<instancia>/mcp/`

## Entrar al panel

Usa el usuario y contraseña definidos en:

- `ADMIN_USER`
- `ADMIN_PASSWORD`

Desde `Settings` puedes cambiar el nombre de usuario y la contraseña del único admin.

## Configurar Easypanel

Ver [Guia de despliegue](./DEPLOYMENT.md).

## Conectar un cliente MCP

Cada instancia configurada expone su propio endpoint MCP. El formato es:

```text
https://tu-dominio/<instancia>/mcp/
```

El endpoint requiere este encabezado:

```text
Authorization: Bearer <MCP_BEARER_TOKEN>
```

Ejemplo:

```bash
curl -H "Authorization: Bearer change_me_long_random_token" https://tu-dominio/compraloahora/mcp/
```

### n8n

Para n8n usa `MCP Client` con:

- `Server Transport`: `HTTP Streamable`
- `MCP Endpoint URL`: `https://tu-dominio/compraloahora/mcp/`
- `Authentication`: `Bearer Auth` o `Header Auth`
- Token o header:
  - `Authorization: Bearer <MCP_BEARER_TOKEN>`

Si el nodo falla con auth, usa `Header Auth` para forzar exactamente ese header.

Las tools ya quedan ligadas a la instancia del endpoint, así que no necesitas pasar `instance` en cada llamada.

### Validado en esta instalación

- Endpoint MCP: `https://dev-odoo-mcp-gateway.ouiteb.easypanel.host/compraloahora/mcp/`
- Instancia activa: `Compraloahora`
- URL Odoo: `https://odoo-concept-compralo-ahora-staging-compraloahora-33072015.dev.odoo.com`
- Version: `17`
- API mode: `xmlrpc`
- `odoo_test_connection`: OK

## Seguridad

- No compartas el `MCP_BEARER_TOKEN`.
- No subas `.env` al repositorio.
- El secreto de cada instancia se guarda cifrado con Fernet.
- En edición, el campo secreto se deja vacío y solo se actualiza si escribes uno nuevo.
- Este proyecto no incluye multiusuario, OAuth, ni permisos avanzados.
