# tools/calculadora.py — aritmética determinista para a Alma: qualquer
# conta com mais do que uma operação trivial passa por aqui, nunca é
# feita "de cabeça" pelo modelo. Pedido do Rui (2026-08-06), depois de um
# erro real de dia da semana levantar a mesma questão sobre números: a
# Alma tem de ser rigorosa nas contas que faz, não só nas datas.
import ast
import operator

_OPERADORES = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _avaliar(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"valor não numérico: {node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERADORES:
        return _OPERADORES[type(node.op)](_avaliar(node.left), _avaliar(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERADORES:
        return _OPERADORES[type(node.op)](_avaliar(node.operand))
    raise ValueError("expressão não suportada — só números, + - * / % ** e parênteses")


def calcular(expressao: str) -> dict:
    """Avalia uma expressão aritmética (ex: "1250 * 1.23", "500+250+250+250",
    "46182 / 10") de forma exata, em código — usa isto sempre que fizeres
    qualquer conta com mais do que uma operação trivial (somas com várias
    parcelas, percentagens, IVA, créditos, médias, divisões): nunca faças
    a conta "de cabeça" e escrevas só o resultado — chama esta função e
    usa exatamente o valor que ela devolve. Só aceita números e os
    operadores + - * / % ** e parênteses — nada de texto, variáveis ou
    funções."""
    try:
        arvore = ast.parse(expressao, mode="eval")
        resultado = _avaliar(arvore.body)
    except ZeroDivisionError:
        return {"erro": "divisão por zero"}
    except Exception as exc:
        return {"erro": f"não consegui calcular {expressao!r}: {exc}"}
    if isinstance(resultado, float) and resultado.is_integer():
        resultado = int(resultado)
    return {"resultado": resultado}


TOOLS_CALCULADORA = [
    {
        "name": "calcular",
        "description": ("Avalia uma expressão aritmética de forma exata — usa isto sempre que fizeres uma "
                        "conta com mais de uma operação (somas com várias parcelas, percentagens, IVA, "
                        "créditos, médias, divisões, etc.): nunca calcules de cabeça e escrevas só o "
                        "resultado, chama esta função e usa exatamente o valor devolvido. Só números e os "
                        "operadores + - * / % ** e parênteses (ex: \"1250 * 1.23\", \"500+250+250+250\", "
                        "\"46182 / 10\")."),
        "input_schema": {
            "type": "object",
            "properties": {
                "expressao": {"type": "string", "description": "expressão aritmética, ex: \"1250 * 1.23\""}
            },
            "required": ["expressao"]
        }
    }
]
