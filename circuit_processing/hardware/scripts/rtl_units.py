"""Structural translation of the bundled EvoApproxLib uint64 Boolean circuits."""
from pathlib import Path
import ast
import re

def _expr(text, names):
    text=re.sub(r'\b(0x[0-9A-Fa-f]+|[0-9]+)[uUlL]*\b',lambda m:str(int(m.group(1),0)) if m.group(1).startswith('0x') else m.group(1),text)
    tree=ast.parse(text.strip(),mode='eval')
    allowed=(ast.Expression,ast.BinOp,ast.UnaryOp,ast.Name,ast.Load,ast.Constant,ast.BitAnd,ast.BitOr,ast.BitXor,ast.LShift,ast.RShift,ast.Invert)
    for node in ast.walk(tree):
        if not isinstance(node,allowed): raise ValueError(f'Unsupported C expression: {text}')
        if isinstance(node,ast.Name) and node.id not in names: raise ValueError(f'Unknown signal {node.id}')
        if isinstance(node,ast.Constant) and (type(node.value) is not int or not 0<=node.value<2**64): raise ValueError('Invalid constant')
    return re.sub(r'\b\d+\b',lambda m:"64'd"+m.group(),text)

def generate_units(root: Path) -> str:
    sources=sorted((root/'third_party/evoapproxlib').glob('add12se_*.c'))
    if len(sources)!=9: raise ValueError('Expected nine bundled approximate adders')
    result=['// Generated from bundled EvoApproxLib C; do not edit.\n']
    for source in sources:
        text=source.read_text(); header=text[:text.index('#include')]
        match=re.search(r'uint64_t\s+(add12se_\w+)\(const uint64_t B,const uint64_t A\)\s*\{(.*?)\}',text,re.S)
        if not match: raise ValueError(f'Unsupported function signature: {source}')
        unit=match[1].split('_')[1]; names={'A','B'}; lines=[]; outputs=[]; returned=False
        for statement in match[2].split(';'):
            statement=statement.strip()
            if not statement: continue
            if statement.startswith('uint64_t '):
                declared=[n.strip() for n in statement[9:].split(',')]
                if any(not re.fullmatch(r'\w+',n) or n in names for n in declared): raise ValueError('Invalid declaration')
                names.update(declared)
                lines.extend('wire [63:0] '+n+';' for n in declared if n!='O')
            elif statement=='O = 0': pass
            elif statement.startswith('O |= '): outputs.append(_expr(statement[5:],names))
            elif statement=='return O': returned=True
            else:
                assignment=re.fullmatch(r'(\w+)\s*=\s*(.*)',statement,re.S)
                if not assignment or assignment[1] not in names or assignment[1] in {'A','B','O'}: raise ValueError(f'Unsupported statement: {statement}')
                lines.append('assign '+assignment[1]+' = '+_expr(assignment[2],names)+';')
        if not returned or len(outputs)!=13: raise ValueError('Unsupported output construction')
        result.append(header+'\nmodule approx_'+unit+'(input wire [11:0] A, B, output wire [12:0] Y);\n'+ '\n'.join(lines)+'\nassign Y = '+ ' | '.join('('+o+')' for o in outputs)+';\nendmodule\n')
    return '\n'.join(result)

