"""Normalize connection identifiers and explain failures without exposing auth payloads."""
import re
from urllib.parse import urlsplit


def normalize_account(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Use a Snowflake account identifier or HTTPS Snowflake account URL.")
        if parsed.hostname == "app.snowflake.com":
            parts = parsed.path.strip("/").split("/")
            if len(parts) < 2 or not all(parts[:2]):
                raise ValueError("The Snowsight URL must include both the organization and account name.")
            value = "-".join(parts[:2])
        elif (parsed.hostname or "").endswith(".snowflakecomputing.com") and parsed.path in ("", "/"):
            value = parsed.hostname.removesuffix(".snowflakecomputing.com")
        else:
            raise ValueError("Use ORG-ACCOUNT or a Snowflake account URL, rather than another website.")
    elif value.lower().endswith(".snowflakecomputing.com"):
        value = value[:-len(".snowflakecomputing.com")]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value) or ".." in value or value.endswith("."):
        raise ValueError("Enter a Snowflake account identifier, such as ORG-ACCOUNT.")
    # ORG.ACCOUNT is Snowflake's SQL/data-sharing spelling. Driver connections use a hyphen.
    # Legacy locator.region[.cloud] names, global URLs, and PrivateLink must keep their dots.
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*)\.([A-Za-z][A-Za-z0-9_]*)", value)
    if match and match[2].lower() not in {"global", "privatelink"}:
        value = f"{match[1]}-{match[2]}"
    return value


def connection_error(exc: Exception) -> str:
    """Classify a private exception chain; return only fixed, safe messages and codes."""
    messages, seen = [], set()
    pending = [exc]
    while pending and len(seen) < 12:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        messages.extend(str(value) for value in (str(current), getattr(current, "msg", ""), getattr(current, "raw_msg", "")))
        pending.extend(item for item in (current.__cause__, current.__context__, *current.args) if isinstance(item, BaseException))
    detail = " ".join(messages).lower()
    unavailable = ("not granted", "not been granted", "not assigned", "not listed", "does not exist", "not authorized", "not exist")
    if "10013" in detail:
        message = "This app process is blocked from connecting to Snowflake (Windows error 10013). Start ClubCal from a terminal with internet access. Your Snowflake login has not been checked."
    elif any(term in detail for term in ("getaddrinfo", "failed to resolve", "name or service not known", "no such host", "dns error")):
        message = "Snowflake's account address could not be resolved. Check the ORG-ACCOUNT identifier and the app's DNS/network access."
    elif "differs from" in detail and ("idp" in detail or "identity provider" in detail):
        message = "The Snowflake username does not match the account signed in through your browser. Enter the matching username or sign in as that user in the browser."
    elif any(word in detail for word in ("warehouse", "database", "schema")) and any(term in detail for term in unavailable):
        message = "Snowflake rejected the warehouse, database, or schema settings. Run the setup SQL in this account and check that your selected role can access those objects."
    elif "role" in detail and any(term in detail for term in unavailable):
        message = "The selected Snowflake role is unavailable to this user. Check the setup script's GRANT ROLE line, or select a role already assigned to your user."
    elif any(term in detail for term in ("insufficient privileges", "access control error", "permission denied")):
        message = "The selected Snowflake role lacks permission for ClubCal's tables. Check database/schema USAGE and CREATE TABLE permissions, and access to any existing ClubCal tables."
    elif any(term in detail for term in ("syntax error", "sql compilation error", "invalid identifier")):
        message = "Snowflake rejected the generated table/query SQL."
        position = re.search(r"line (\d+) at position (\d+)", detail)
        if position:
            message += f" SQL position: line {position[1]}, column {position[2]}."
        unexpected = re.search(r"unexpected ['\"]([a-z_]+)['\"]", detail)
        if unexpected and unexpected[1] in {"workspace", "event_id", "payload", "state", "created_at", "updated_at", "post_id", "processing_key", "content_hash", "result", "warnings", "cache_valid", "import_id", "started_at", "primary", "key", "varchar", "boolean"}:
            message += f" Unexpected SQL token: {unexpected[1].upper()}."
    elif any(term in detail for term in ("network policy", "ip is not allowed", "ip address is not allowed", "ip address is blocked")):
        message = "Snowflake's network policy rejected this connection. Ask your Snowflake administrator to check access for this computer's network."
    elif "saml" in detail and any(term in detail for term in ("identity provider", "identity_provider", "authenticator request")):
        message = "Snowflake rejected the organization's SAML identity-provider configuration. Select Browser sign-in for Snowflake's local OAuth login, or ask your administrator to check organization SSO."
    elif any(term in detail for term in ("ssl", "certificate", "tls", "ocsp")):
        message = "Snowflake's secure connection could not be verified. Check the account address and the computer's certificate/proxy configuration."
    elif "404" in detail or "account not found" in detail:
        message = "Snowflake could not find that account endpoint. Copy the client account identifier (ORG-ACCOUNT) from Snowflake's account details."
    elif any(term in detail for term in ("incorrect username", "invalid username", "user does not exist", "authentication failed", "invalid credentials")):
        message = "Snowflake rejected the sign-in. Check the username for this account and complete the selected authentication method."
    elif ("browser" in detail and any(term in detail for term in ("could not open", "failed to open", "cannot open", "could not launch", "failed to launch"))):
        message = "The app could not open Snowflake's browser sign-in. Start ClubCal on the computer where you can open the browser and complete login."
    elif any(term in detail for term in ("timed out", "timeout", "time out")):
        message = "The Snowflake connection or browser sign-in timed out. Complete login/MFA in the browser and check the app's network access before retrying."
    elif any(term in detail for term in ("private key", "key pair", "key-pair", "jwt")):
        message = "Snowflake key-pair authentication failed. Check the local key file, passphrase, and registered public key, or select Browser sign-in."
    elif any(term in detail for term in ("connection refused", "connection reset", "remote disconnected", "failed to connect", "error sending request", "could not connect")):
        message = "The app could not reach Snowflake successfully. Check the account identifier, network/firewall/proxy access, and any browser sign-in prompt."
    else:
        message = "Snowflake could not establish a session. Check the ORG-ACCOUNT identifier, username, authentication method, and any browser sign-in prompt."
    code = getattr(exc, "errno", None)
    state = getattr(exc, "sqlstate", None)
    codes = [f"code {code}"] if isinstance(code, int) else []
    if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
        codes.append(f"SQLSTATE {state}")
    return message + (f" ({'; '.join(codes)})" if codes else "")


def inspect_resources(store, settings) -> list[dict]:
    """Read only the configured resources visible to the authenticated role."""
    settings.validate()
    def visible(query, expected):
        cursor = store.connection.cursor()
        try:
            store._execute(cursor, query, [expected])
            columns = [column[0].lower() for column in cursor.description]
            name_index = columns.index("name")
            return any(str(row[name_index]).upper() == expected.upper() for row in cursor.fetchall())
        finally:
            cursor.close()
    database = visible("SHOW DATABASES LIKE ?", settings.database)
    schema = visible(f'SHOW SCHEMAS LIKE ? IN DATABASE "{settings.database.upper()}"', settings.schema) if database else None
    warehouse = visible("SHOW WAREHOUSES LIKE ?", settings.warehouse)
    return [{"resource": f"Database {settings.database}", "available": database},
            {"resource": f"Schema {settings.database}.{settings.schema}", "available": schema},
            {"resource": f"Warehouse {settings.warehouse}", "available": warehouse}]


def resource_setup_sql(settings) -> list[str]:
    settings.validate()
    return [f'CREATE DATABASE IF NOT EXISTS "{settings.database.upper()}"',
            f'CREATE SCHEMA IF NOT EXISTS "{settings.database.upper()}"."{settings.schema.upper()}"']


def initialize_database_schema(store, settings) -> None:
    """Run only the two reviewed statements after explicit confirmation in the UI."""
    statements = resource_setup_sql(settings)
    cursor = store.connection.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
    finally:
        cursor.close()
