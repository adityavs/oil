# Test var / setvar / etc.

# TODO: GetVar needs a mode where Obj[str] gets translated to value.Str?
# Then all code will work.
#
# word_eval:
#
# val = self.mem.GetVar(var_name) ->
# val = GetWordVar(self.mem, var_name)
#
# Conversely, in oil_lang/expr_eval.py:
# LookupVar gives you a plain Python object.  I don't think there's any
# downside here.
#
# repr exposes the differences.
#
# Notes:
#
# - osh/cmd_exec.py handles OilAssign, which gets wrapped in value.Obj()
# - osh/word_eval.py _ValueToPartValue handles 3 value types.  Used in:
#   - _EvalBracedVarSub
#   - SimpleVarSub in _EvalWordPart
# - osh/expr_eval.py: _LookupVar wrapper should disallow using Oil values
#   - this is legacy stuff.  Both (( )) and [[ ]]
#   - LhsIndexedName should not reference Oil vars either

#### Set $HOME using 'var' (using Oil var in word evaluator)
var HOME = "foo"
echo $HOME
echo ~
## STDOUT:
foo
foo
## END

#### Splice in regular array (using Oil var in word evaluator)
shopt -s oil-parse-at static-word-eval
var mylist = ["one", "two"]
argv.py @mylist
## STDOUT:
['one', 'two']
## END

#### Use shell var in Oil expression
x='abc'
var length = len(x)  # length in BYTES, unlike ${#x}
echo $length
## STDOUT:
3
## END