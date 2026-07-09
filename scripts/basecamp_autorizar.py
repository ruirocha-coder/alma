"""
Autorização OAuth2 ao Basecamp — corre-se UMA VEZ, localmente, para obter o
refresh_token que a Alma vai usar para sempre (o refresh_token não expira,
ao contrário do access_token, que dura ~2 semanas e é renovado automaticamente).

Pré-requisito: teres criado uma integração em
https://launchpad.37signals.com/integrations e teres o Client ID e Client
Secret dela.

Uso:
    python scripts/basecamp_autorizar.py

Segue as instruções no ecrã. No fim, o script mostra as variáveis de
ambiente a adicionar no Railway.
"""
import httpx

REDIRECT_URI = "http://localhost"

def main():
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()

    autorizar_url = (
        "https://launchpad.37signals.com/authorization/new"
        f"?type=web_server&client_id={client_id}&redirect_uri={REDIRECT_URI}"
    )
    print("\n1. Abre este link no browser, faz login e clica em 'Yes, I'll allow access':\n")
    print(f"   {autorizar_url}\n")
    print("2. Vais ser redirecionado para um URL do género http://localhost/?code=XXXX")
    print("   (o browser vai mostrar erro de ligação — é normal, não há servidor ali;")
    print("   o que interessa é o 'code' que aparece na barra de endereço.)\n")

    code = input("Cola aqui o valor do 'code': ").strip()

    r = httpx.post(
        "https://launchpad.37signals.com/authorization/token",
        data={
            "type": "web_server",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
    )
    r.raise_for_status()
    tokens = r.json()

    contas = httpx.get(
        "https://launchpad.37signals.com/authorization.json",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    ).json()

    print("\nContas/produtos a que este token tem acesso:")
    for conta in contas.get("accounts", []):
        print(f"  - id={conta['id']}  nome={conta['name']}  produto={conta['product']}")

    print("\nVariáveis de ambiente a adicionar no Railway:\n")
    print(f"BASECAMP_CLIENT_ID={client_id}")
    print(f"BASECAMP_CLIENT_SECRET={client_secret}")
    print(f"BASECAMP_REFRESH_TOKEN={tokens['refresh_token']}")
    print("BASECAMP_ACCOUNT_ID=<o id da conta 'bc3' na lista acima>")

if __name__ == "__main__":
    main()
