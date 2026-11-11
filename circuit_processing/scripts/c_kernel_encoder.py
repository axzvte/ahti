"""Compile a deliberately bounded C kernel subset to the shared scalar SSA IR.

No compiler execution or application-specific graph templates are used. Loops and
array addresses must be statically resolvable; unsupported C fails closed.
"""
from __future__ import annotations
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from pycparser import c_ast as A, c_parser


class EncodingError(ValueError):
    pass


INTRINSICS = {'circuit_add','circuit_sub','circuit_abs','circuit_clip',
              'circuit_round_shift','circuit_round_even','circuit_trunc'}


@dataclass(frozen=True)
class Value:
    value: object
    integer: bool = True
    width: int = 32


class Returned(Exception):
    def __init__(self, value):
        self.value = value


class Encoder:
    def __init__(self, descriptor, path, source):
        self.d, self.path, self.source = descriptor, path, source
        self.ops, self.groups, self.frames, self.functions = [], {}, [{}], {}
        self.depth = 0
        self.call_stack = []
        self.storage_types = {}
        self.readonly = set()

    def register_storage(self, container, key, typ, readonly=False):
        self.storage_types[(id(container),key)]=typ
        if isinstance(typ,A.ArrayDecl):
            for i in range(len(container[key])):
                self.register_storage(container[key],i,typ.type,readonly)
        else:
            self.typ(typ)  # reject unsupported declarations even when uninitialized
            self.storage_types[(id(container),key)]=typ
            self.readonly.discard((id(container),key))
            if readonly or 'const' in (getattr(typ,'quals',None) or []):
                self.readonly.add((id(container),key))

    def fail(self, node, message):
        raise EncodingError(f"{getattr(node, 'coord', None) or self.path}: {message}")

    def constant(self, value, node):
        if not isinstance(value, Value) or isinstance(value.value, str):
            self.fail(node, 'requires a compile-time scalar constant (dynamic control/address is unsupported)')
        return value.value

    def lookup(self, name, node):
        for frame in reversed(self.frames):
            if name in frame:
                return frame[name]
        self.fail(node, f'unknown identifier {name}')

    def emit(self, op, args, node, integer=None, width=None, **extra):
        if len(self.ops) >= 200000:
            self.fail(node, 'expanded operation budget exceeded')
        if not all(isinstance(v, Value) for v in args):
            self.fail(node, 'scalar operands required')
        integer = all(v.integer for v in args) if integer is None else integer
        width = max((v.width for v in args), default=32) if width is None else width
        ident = f'n{len(self.ops)}'
        self.ops.append(dict(id=ident, op=op, args=[v.value for v in args],
                             integer=integer, width=width, signed=True,
                             source_coord=str(getattr(node,'coord',None) or self.path),
                             call_stack=list(self.call_stack), **extra))
        return Value(ident, integer, width)

    def typ(self, node):
        while isinstance(node, (A.TypeDecl, A.Typename, A.Decl)):
            node = node.type
        if not isinstance(node, A.IdentifierType):
            self.fail(node, 'only signed scalar types and fixed arrays are supported')
        names = node.names
        if any(x not in ('int', 'long', 'signed', 'double', 'void') for x in names):
            self.fail(node, f'unsupported type {names}')
        return 'double' not in names, 64 if 'long' in names or 'double' in names else 32

    def cast(self, value, typ, node):
        return self.convert(value,*self.typ(typ),node)

    def convert(self, value, integer, width, node):
        if value is None:
            self.fail(node, 'read of uninitialized value')
        if not isinstance(value, Value):
            self.fail(node, 'array cannot be assigned to scalar')
        if isinstance(value.value, (int, float)):
            result = math.trunc(value.value) if integer else float(value.value)
            if abs(result) > 2**40 or (integer and not -(2**(width-1)) <= result < 2**(width-1)):
                self.fail(node, 'constant outside supported signed numeric range')
            return Value(result, integer, width)
        if integer and not value.integer:
            return self.emit('trunc', [value], node, integer=True, width=width)
        if integer == value.integer and width == value.width:
            return value
        return self.emit('scale', [value], node, integer=integer, width=width, factor=1)

    def binary(self, op, x, y, node, marked=None):
        if not isinstance(x, Value) or not isinstance(y, Value):
            self.fail(node, 'scalar operands required')
        a, b = x.value, y.value
        static = not isinstance(a, str) and not isinstance(b, str)
        if static and marked is None:
            funcs = {'+':lambda:a+b, '-':lambda:a-b, '*':lambda:a*b,
                     '/':lambda:math.trunc(a/b) if x.integer and y.integer else a/b,
                     '%':lambda:a-math.trunc(a/b)*b, '<':lambda:int(a<b),
                     '<=':lambda:int(a<=b), '>':lambda:int(a>b), '>=':lambda:int(a>=b),
                     '==':lambda:int(a==b), '!=':lambda:int(a!=b),
                     '&&':lambda:int(bool(a) and bool(b)), '||':lambda:int(bool(a) or bool(b)),
                     '<<':lambda:a<<b, '>>':lambda:a>>b}
            if op not in funcs:
                self.fail(node, f'unsupported operator {op}')
            try:
                val = funcs[op]()
            except (ValueError, ZeroDivisionError, TypeError, OverflowError):
                self.fail(node, 'invalid constant arithmetic')
            if not math.isfinite(val) or abs(val) > 2**40:
                self.fail(node, 'constant outside supported numeric range')
            return Value(val, x.integer and y.integer, max(x.width,y.width))
        if op in ('+', '-'):
            extra = {}
            if marked is not None:
                extra['unit_node'] = marked
            elif self.d['node_policy'] == 'all_integer_adds' and x.integer and y.integer:
                key = str(node.coord)
                extra['unit_node'] = self.groups.setdefault(key, len(self.groups))
            return self.emit('add' if op == '+' else 'sub', [x,y], node, **extra)
        if op == '*':
            if isinstance(a, str) and isinstance(b, str):
                self.fail(node, 'runtime multiplication requires a constant operand')
            factor, arg = (a,y) if not isinstance(a,str) else (b,x)
            return self.emit('scale', [arg], node, integer=x.integer and y.integer, factor=factor)
        if op in ('<<','>>') and x.integer and y.integer:
            bits = self.constant(y,node)
            if not isinstance(bits,int) or not 0 <= bits < x.width:
                self.fail(node, 'shift count outside type width')
            if op=='<<':
                if bits>40: self.fail(node,'left-shift scale factor outside supported numeric range')
                return self.emit('scale',[x],node,factor=1<<bits)
            return self.emit('shift', [x], node, bits=bits)
        self.fail(node, f'unsupported runtime operator {op}')

    def slot(self, node):
        if isinstance(node,A.ID):
            for frame in reversed(self.frames):
                if node.name in frame:
                    return frame,node.name
            self.fail(node,'assignment to undeclared identifier')
        if isinstance(node,A.ArrayRef):
            arr = self.expr(node.name)
            index = self.constant(self.expr(node.subscript),node)
            if not isinstance(arr,list) or not isinstance(index,int) or not 0 <= index < len(arr):
                self.fail(node,'array index out of bounds or non-array access')
            return arr,index
        self.fail(node,'unsupported assignment target (pointers are unsupported)')

    def declared_type(self,node):
        """Inspect an lvalue's declaration without evaluating any index or value."""
        if isinstance(node,A.ID):
            for frame in reversed(self.frames):
                if node.name in frame:
                    typ=self.storage_types.get((id(frame),node.name))
                    if typ is None: self.fail(node,'missing declared type')
                    return typ
            self.fail(node,'unknown identifier in type inference')
        if isinstance(node,A.ArrayRef):
            typ=self.declared_type(node.name)
            if not isinstance(typ,A.ArrayDecl): self.fail(node,'non-array type in array reference')
            return typ.type
        self.fail(node,'cannot infer lvalue type without evaluating expression')

    def expression_type(self,node):
        """C scalar type inference. Never executes the inactive ternary branch."""
        def usual(left,right):
            return (left[0] and right[0],max(left[1],right[1]))
        if isinstance(node,(A.ID,A.ArrayRef)):
            return self.typ(self.declared_type(node))
        if isinstance(node,A.Constant):
            if node.type=='double': return False,64
            if node.type in ('int','long int','long long int'):
                return True,64 if 'long' in node.type else 32
            self.fail(node,'unsupported literal type in conditional expression')
        if isinstance(node,A.Cast): return self.typ(node.to_type)
        if isinstance(node,A.UnaryOp):
            if node.op=='!': return True,32
            if node.op in ('+','-','++','--','p++','p--'):
                return self.expression_type(node.expr)
            self.fail(node,'unsupported unary expression type')
        if isinstance(node,A.BinaryOp):
            left,right=self.expression_type(node.left),self.expression_type(node.right)
            if node.op in ('<','<=','>','>=','==','!=','&&','||'): return True,32
            if node.op in ('<<','>>'): return left
            if node.op in ('+','-','*','/','%'): return usual(left,right)
            self.fail(node,'unsupported binary expression type')
        if isinstance(node,A.TernaryOp):
            return usual(self.expression_type(node.iftrue),self.expression_type(node.iffalse))
        if isinstance(node,A.FuncCall) and isinstance(node.name,A.ID):
            name=node.name.name
            if name in INTRINSICS:
                if name in ('circuit_add','circuit_sub','circuit_abs'): return True,32
                if name=='circuit_round_shift': return True,64
                return False,64
            if name in self.functions: return self.typ(self.functions[name].decl.type.type)
            self.fail(node,'unknown function in conditional type inference')
        self.fail(node,f'cannot infer conditional branch type {type(node).__name__}')

    def expr(self,node):
        if isinstance(node,A.Constant):
            if node.type in ('float','char') or 'unsigned' in node.type:
                self.fail(node,'float32, character and unsigned literals are unsupported; use double/signed integers')
            raw = re.sub(r'[uUlLfF]+$', '', node.value)
            try:
                val=float(raw) if node.type=='double' else int(raw,0)
                if not math.isfinite(val) or abs(val)>2**40: self.fail(node,'literal outside supported numeric range')
                return Value(val,node.type!='double',64 if node.type=='double' or 'long' in node.type else 32)
            except ValueError:
                self.fail(node,'unsupported literal')
        if isinstance(node,A.ID):
            val = self.lookup(node.name,node)
            if val is None:
                self.fail(node,'read of uninitialized scalar')
            return val
        if isinstance(node,A.ArrayRef):
            arr,key = self.slot(node)
            if arr[key] is None:
                self.fail(node,'read of uninitialized array element')
            return arr[key]
        if isinstance(node,A.BinaryOp):
            if node.op in ('&&','||'):
                left=self.constant(self.expr(node.left),node.left)
                if node.op=='&&' and not left: return Value(0)
                if node.op=='||' and left: return Value(1)
                return Value(int(bool(self.constant(self.expr(node.right),node.right))))
            return self.binary(node.op,self.expr(node.left),self.expr(node.right),node)
        if isinstance(node,A.Cast):
            return self.cast(self.expr(node.expr),node.to_type,node)
        if isinstance(node,A.TernaryOp):
            typ=self.expression_type(node)
            selected=node.iftrue if self.constant(self.expr(node.cond),node) else node.iffalse
            return self.convert(self.expr(selected),*typ,node)
        if isinstance(node,A.UnaryOp):
            if node.op in ('++','p++','--','p--'):
                arr,key=self.slot(node.expr)
                if (id(arr),key) in self.readonly: self.fail(node,'assignment to const storage')
                old=arr[key]
                arr[key]=self.binary('+',old,Value(1 if '+' in node.op else -1),node)
                return old if node.op.startswith('p') else arr[key]
            value=self.expr(node.expr)
            if node.op=='+': return value
            if node.op=='-': return self.binary('*',Value(-1),value,node)
            if node.op=='!': return Value(int(not self.constant(value,node)))
            self.fail(node,f'unsupported unary operator {node.op}')
        if isinstance(node,A.FuncCall):
            if not isinstance(node.name,A.ID): self.fail(node,'indirect calls unsupported')
            name=node.name.name
            args=[self.expr(x) for x in (node.args.exprs if node.args else [])]
            arities={'circuit_add':3,'circuit_sub':3,'circuit_abs':1,'circuit_clip':3,
                     'circuit_round_shift':2,'circuit_round_even':1,'circuit_trunc':1}
            if name in arities:
                if len(args)!=arities[name]: self.fail(node,'incorrect intrinsic arity')
                # Match the trusted header's C parameter conversions before
                # lowering intrinsics. Value.integer tracks the C scalar type.
                formal_types={
                    'circuit_add':[(True,32)]*3,
                    'circuit_sub':[(True,32)]*3,
                    'circuit_abs':[(True,32)],
                    'circuit_clip':[(False,64)]*3,
                    'circuit_round_shift':[(True,64),(True,32)],
                    'circuit_round_even':[(False,64)],
                    'circuit_trunc':[(False,64)]}
                args=[self.convert(arg,*typ,node) for arg,typ in zip(args,formal_types[name])]
                if name in ('circuit_add','circuit_sub'):
                    group=self.constant(args[0],node)
                    if not isinstance(group,int) or group<0 or not all(v.integer for v in args[1:]):
                        self.fail(node,'marked add/sub requires nonnegative constant group and integer operands')
                    return self.binary('+' if name=='circuit_add' else '-',*args[1:],node,marked=group)
                op={'circuit_abs':'abs','circuit_clip':'clamp','circuit_round_shift':'round_shift',
                    'circuit_round_even':'round_even','circuit_trunc':'trunc'}[name]
                extra={}
                if op=='clamp':
                    extra=dict(min=self.constant(args[1],node),max=self.constant(args[2],node))
                    if extra['min']>extra['max']: self.fail(node,'invalid clamp bounds')
                if op=='round_shift':
                    bits=self.constant(args[1],node)
                    if not isinstance(bits,int) or not 0<=bits<64 or not args[0].integer:
                        self.fail(node,'round_shift needs integer operand and constant bits 0..63')
                    extra=dict(bits=bits)
                result=self.emit(op,args[:1],node,
                                 integer=op!='clamp',
                                 width=32 if op=='abs' else 64,**extra)
                # round_even/trunc are mathematically integral operations, but
                # their C return type is double; downstream C additions must
                # retain floating semantics and must not become integer groups.
                if name in ('circuit_clip','circuit_round_even','circuit_trunc'):
                    return Value(result.value,False,64)
                return result
            return self.call(name,args,node)
        self.fail(node,f'unsupported expression {type(node).__name__}')

    def declaration(self,node):
        if node.storage:
            self.fail(node,'storage-class declarations are unsupported (persistent local state is not encodable)')
        def allocate(typ,init):
            if isinstance(typ,A.ArrayDecl):
                size=self.constant(self.expr(typ.dim),node)
                if not isinstance(size,int) or not 0<size<=65536: self.fail(node,'invalid fixed array size')
                if init is not None and not isinstance(init,A.InitList): self.fail(node,'array initializer must be a list')
                exprs=init.exprs if init else []
                if len(exprs)>size: self.fail(node,'excess array initializers')
                return [allocate(typ.type,exprs[i] if i<len(exprs) else A.Constant('int','0') if init else None) for i in range(size)]
            return self.cast(self.expr(init),typ,node) if init is not None else None
        self.frames[-1][node.name]=allocate(node.type,node.init)
        self.register_storage(self.frames[-1],node.name,node.type,'const' in (node.quals or []))

    def stmt(self,node):
        if node is None: return
        if isinstance(node,A.Compound):
            self.frames.append({})
            try:
                for child in node.block_items or []: self.stmt(child)
            finally: self.frames.pop()
        elif isinstance(node,A.Decl): self.declaration(node)
        elif isinstance(node,A.DeclList):
            for child in node.decls: self.declaration(child)
        elif isinstance(node,A.Assignment):
            arr,key=self.slot(node.lvalue)
            if (id(arr),key) in self.readonly: self.fail(node,'assignment to const storage')
            val=self.expr(node.rvalue)
            if node.op!='=': val=self.binary(node.op[:-1],arr[key],val,node)
            typ=self.storage_types.get((id(arr),key))
            if typ is not None: val=self.cast(val,typ,node)
            arr[key]=val
        elif isinstance(node,A.For):
            self.frames.append({})
            try:
                self.stmt(node.init)
                count=0
                while self.constant(self.expr(node.cond),node):
                    count+=1
                    if count>4096: self.fail(node,'fixed loop expansion exceeds 4096 iterations')
                    self.stmt(node.stmt); self.stmt(node.next)
            finally: self.frames.pop()
        elif isinstance(node,A.If):
            self.stmt(node.iftrue if self.constant(self.expr(node.cond),node) else node.iffalse)
        elif isinstance(node,A.Return): raise Returned(self.expr(node.expr) if node.expr else None)
        elif isinstance(node,(A.FuncCall,A.UnaryOp)): self.expr(node)
        elif isinstance(node,A.EmptyStatement): pass
        else: self.fail(node,f'unsupported statement {type(node).__name__}')

    def call(self,name,args,node):
        if name not in self.functions: self.fail(node,f'unknown function {name}')
        if self.depth>=16: self.fail(node,'recursive/deep function expansion unsupported')
        fn=self.functions[name]
        params=fn.decl.type.args.params if fn.decl.type.args else []
        if len(params)!=len(args): self.fail(node,'function arity mismatch')
        frame={}
        for param,arg in zip(params,args):
            if isinstance(param.type,A.ArrayDecl):
                size=self.constant(self.expr(param.type.dim),param)
                if not isinstance(arg,list) or len(arg)!=size: self.fail(node,'fixed array parameter size mismatch')
                frame[param.name]=arg
            else: frame[param.name]=self.cast(arg,param.type,param)
            self.register_storage(frame,param.name,param.type,'const' in (param.quals or []))
        # Function bodies can see globals, but never caller-local identifiers.
        saved=self.frames
        self.frames=[saved[0],frame]
        self.depth+=1
        self.call_stack.append(dict(function=name,call_coord=str(getattr(node,'coord',None) or self.path)))
        try:
            try: self.stmt(fn.body)
            except Returned as result:
                return_type=fn.decl.type.type
                if result.value is not None: return self.cast(result.value,return_type,node)
                return None
        finally:
            self.frames=saved; self.depth-=1; self.call_stack.pop()
        return None


def encode_profile(descriptor: dict, base_dir: Path) -> dict:
    """Read a schema-2 C descriptor and produce a deterministic schema-3 profile."""
    if descriptor.get('application')!='c' or descriptor.get('schema')!=2:
        raise EncodingError('expected schema 2 C source descriptor')
    if descriptor.get('node_policy') not in ('marked','all_integer_adds'):
        raise EncodingError('node_policy must be marked or all_integer_adds')
    path=(Path(base_dir)/descriptor['source']).resolve()
    raw=path.read_bytes(); source=raw.decode('utf-8-sig')
    # Includes are never followed. Only our intrinsic declaration header is allowed.
    def directive(match):
        if not re.fullmatch(r'\s*#\s*include\s+"circuit_intrinsics.h"\s*',match.group()):
            raise EncodingError(f'{path}: unsupported preprocessor directive')
        return ''
    source=re.sub(r'^\s*#.*$',directive,source,flags=re.M)
    source=re.sub(r'/\*.*?\*/|//[^\n]*',lambda m:'\n'*m.group().count('\n'),source,flags=re.S)
    enc=Encoder(descriptor,path,source)
    try: ast=c_parser.CParser().parse(source,filename=str(path))
    except Exception as error: raise EncodingError(str(error)) from error
    for item in ast.ext:
        if isinstance(item,A.FuncDef):
            if item.decl.name in INTRINSICS:
                enc.fail(item,'reserved intrinsic function definitions are unsupported; use the trusted intrinsic header')
            if item.decl.name in enc.functions: enc.fail(item,'duplicate function')
            enc.functions[item.decl.name]=item
        elif isinstance(item,A.Decl) and not isinstance(item.type,A.FuncDecl):
            if 'const' not in (item.quals or []):
                enc.fail(item,'mutable globals are unsupported; only initialized const globals are allowed')
            if item.init is None:
                enc.fail(item,'const globals require an explicit initializer')
            enc.declaration(item)
        elif isinstance(item,A.Decl) and isinstance(item.type,A.FuncDecl): pass
        else: enc.fail(item,'unsupported top-level declaration')
    inputs=[Value(x['id']) for x in descriptor['inputs']]
    output=[None]*len(descriptor['outputs'])
    fn=enc.functions.get(descriptor['entry'])
    if fn is None: enc.fail(ast,'entry function not found')
    params=fn.decl.type.args.params if fn.decl.type.args else []
    if len(params)!=2 or not all(isinstance(p.type,A.ArrayDecl) for p in params):
        enc.fail(fn,'entry must take two fixed arrays: signed int input[N], double output[M]')
    if enc.typ(params[0].type.type)!=(True,32) or enc.typ(params[1].type.type)!=(False,64):
        enc.fail(fn,'entry requires signed int input[N] and double output[M]')
    ret=fn.decl.type.type
    while isinstance(ret,A.TypeDecl): ret=ret.type
    if not isinstance(ret,A.IdentifierType) or ret.names!=['void']:
        enc.fail(fn,'entry must return void')
    enc.call(descriptor['entry'],[inputs,output],ast)
    if any(not isinstance(x,Value) for x in output): enc.fail(ast,'every output must be assigned')
    ir=dict(schema=1,tile=descriptor['tile'],border=descriptor['border'],
            interior_only=descriptor.get('interior_only',False),
            border_output=descriptor.get('border_output','input'),
            inputs=descriptor['inputs'],operations=enc.ops,
            outputs=[dict(value=v.value,**offset) for v,offset in zip(output,descriptor['outputs'])])
    baseline=descriptor.get('baseline')
    if baseline is None:
        baseline=[dict(node=group,unit='exact') for group in sorted({op['unit_node'] for op in enc.ops if 'unit_node' in op})]
    return dict(schema=3,name=descriptor['name'],application='ir',baseline=baseline,ir=ir,
                source=dict(sha256=hashlib.sha256(raw).hexdigest(),path=descriptor['source'],
                            entry=descriptor['entry'],node_policy=descriptor['node_policy'],frontend='pycparser',
                            language='bounded-c-v1'))
