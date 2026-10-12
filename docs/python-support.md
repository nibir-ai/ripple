# Supported Python syntax and counterexamples

| Case | Supported static evidence | Counterexample behavior |
|---|---|---|
| Module and async functions | Unique direct lexical call | Duplicate/conditional definition unresolved |
| Nested functions | Nearest lexical definition; closure/recursion | Parameter, assignment, deleted binding blocks fallback |
| Global/nonlocal | Read-only declaration selects module/enclosing function | Rebinding invalidates destination and sibling readers |
| Method free names | Skip class namespace; use enclosing function/module | Class member name is not a free variable |
| From-import | Absolute/relative/parent imports and aliases | Conditional, wildcard, missing module unresolved |
| Module import | `import pkg.mod`, `as alias`, explicit imported submodule | File existence alone does not bind a child module |
| Source roots | Auto src, explicit roots, namespace packages | Duplicate modules or nonpackage parent unresolved |
| Self/cls methods | Unique lexical member of base-free, undecorated class | Unknown receiver, rebinding, inheritance, custom decorators unresolved |
| Class-name methods | Known local/imported class member | Shadowed class name or decorator blocks target |
| Builtin method decorators | Unshadowed staticmethod/classmethod | Custom/deleted/assigned decorator not assumed builtin |
| Constructors | Call points to class symbol | No manufactured `__init__`, `__new__` or metaclass call chain |
| External calls | Known stdlib/builtin target outside local scope | Missing third-party import stays unresolved |
| Dynamic imports/mutation | Unknown diagnostic; known attribute writes invalidate members | No type/name heuristic creates a target |
| Decorator/base references | Typed DECORATES/INHERITS with correct definition owner | Never propagate as CALLS |
| Source positions | Encoding cookies, BOM, CRLF, UTF-8 byte columns, decorator @ token | Newer unsupported syntax is diagnosed by host parser |

Lambda/comprehension call dispatch, annotation/default evaluation, generic annotation scopes,
re-exports, alias assignment/type inference, inheritance/MRO, `super`, framework callbacks
and descriptors are intentionally not inferred. Calls at these sites remain unresolved.
Attribute reads and callback argument references are not yet a comprehensive reference index.

The tests contain handwritten positive and negative graphs for the published rules.
There is no empirical accuracy percentage or whole-program runtime completeness claim.
