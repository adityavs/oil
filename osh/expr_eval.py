#!/usr/bin/env python2
# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
expr_eval.py -- Currently used for boolean and arithmetic expressions.
"""

import stat

from _devbuild.gen.id_kind_asdl import Id
from _devbuild.gen.id_tables import BOOL_ARG_TYPES
from _devbuild.gen.runtime_asdl import (
    scope_e, lvalue, quote_e, quote_t,
    value, value_e, value_t, value__MaybeStrArray, value__AssocArray,
)
from _devbuild.gen.syntax_asdl import (
    arith_expr_e, sh_lhs_expr_e, sh_lhs_expr_t, bool_expr_e, word_t
)
from _devbuild.gen.types_asdl import bool_arg_type_e
from asdl import runtime
from core import error
from core.util import e_die
from osh import state
from osh import word_

from mycpp.mylib import tagswitch, switch

import posix_ as posix
try:
  import libc  # for fnmatch
except ImportError:
  from benchmarks import fake_libc as libc  # type: ignore

from typing import Tuple, cast, Any, TYPE_CHECKING
if TYPE_CHECKING:
  from _devbuild.gen.syntax_asdl import bool_expr_t, arith_expr_t, sh_lhs_expr_t
  from _devbuild.gen.runtime_asdl import lvalue_t, scope_t
  from core.ui import ErrorFormatter
  from osh.state import Mem, ExecOpts
  from osh import word_eval


def _StringToInteger(s, span_id=runtime.NO_SPID):
  """Use bash-like rules to coerce a string to an integer.

  Runtime parsing enables silly stuff like $(( $(echo 1)$(echo 2) + 1 )) => 13

  0xAB -- hex constant
  042  -- octal constant
  42   -- decimal constant
  64#z -- arbitary base constant

  bare word: variable
  quoted word: string (not done?)
  """
  if s.startswith('0x'):
    try:
      integer = int(s, 16)
    except ValueError:
      e_die('Invalid hex constant %r', s, span_id=span_id)
    return integer

  if s.startswith('0'):
    try:
      integer = int(s, 8)
    except ValueError:
      e_die('Invalid octal constant %r', s, span_id=span_id)
    return integer

  if '#' in s:
    b, digits = s.split('#', 1)
    try:
      base = int(b)
    except ValueError:
      e_die('Invalid base for numeric constant %r',  b, span_id=span_id)

    integer = 0
    n = 1
    for char in digits:
      if 'a' <= char and char <= 'z':
        digit = ord(char) - ord('a') + 10
      elif 'A' <= char and char <= 'Z':
        digit = ord(char) - ord('A') + 36
      elif char == '@':  # horrible syntax
        digit = 62
      elif char == '_':
        digit = 63
      elif char.isdigit():
        digit = int(char)
      else:
        e_die('Invalid digits for numeric constant %r', digits, span_id=span_id)

      if digit >= base:
        e_die('Digits %r out of range for base %d', digits, base, span_id=span_id)

      integer += digit * n
      n *= base
    return integer

  # Normal base 10 integer
  try:
    integer = int(s)
  except ValueError:
    e_die("Invalid integer constant %r", s, span_id=span_id)
  return integer


#
# Common logic for Arith and Command/Word variants of the same expression
#
# Calls EvalLhs()
#   a[$key]=$val                 # osh/cmd_exec.py:814  (command_e.ShAssignment)
# Calls _EvalLhsArith()
#   (( a[key] = val ))           # osh/expr_eval.py:326 (_EvalLhsArith)
#
# Calls EvalLhsAndLookup():
#   a[$key]+=$val                # osh/cmd_exec.py:795  (assign_op_e.PlusEqual)
#   (( a[key] += val ))          # osh/expr_eval.py:308 (_EvalLhsAndLookupArith)
#
# Uses Python's [] operator
#   val=${a[$key]}               # osh/word_eval.py:639 (bracket_op_e.ArrayIndex)
#   (( val = a[key] ))           # osh/expr_eval.py:509 (Id.Arith_LBracket)
#


def _LookupVar(name, mem, exec_opts):
  val = mem.GetVar(name)
  # By default, undefined variables are the ZERO value.  TODO: Respect
  # nounset and raise an exception.
  if val.tag == value_e.Undef and exec_opts.nounset:
    e_die('Undefined variable %r', name)  # TODO: need token
  return val


def EvalLhs(node, arith_ev, mem, spid, lookup_mode):
  # type: (sh_lhs_expr_t, ArithEvaluator, Mem, int, scope_t) -> lvalue_t
  """sh_lhs_expr -> lvalue.

  Used for a=b and a[x]=b
  """
  assert isinstance(node, sh_lhs_expr_t), node

  if node.tag == sh_lhs_expr_e.Name:  # a=x
    lval = lvalue.Named(node.name)
    lval.spids.append(spid)

  elif node.tag == sh_lhs_expr_e.IndexedName:  # a[1+2]=x

    if mem.IsAssocArray(node.name, lookup_mode):
      key = arith_ev.EvalWordToString(node.index)
      lval = lvalue.Keyed(node.name, key)
      lval.spids.append(node.spids[0])  # copy left-most token over
    else:
      index = arith_ev.EvalToInt(node.index)
      lval = lvalue.Indexed(node.name, index)
      lval.spids.append(node.spids[0])  # copy left-most token over

  else:
    raise AssertionError(node.tag)

  return lval


def _EvalLhsArith(node, mem, arith_ev):
  """sh_lhs_expr -> lvalue.
  
  Very similar to EvalLhs above in core/cmd_exec.
  """
  assert isinstance(node, sh_lhs_expr_t), node

  if node.tag == sh_lhs_expr_e.Name:  # (( i = 42 ))
    lval = lvalue.Named(node.name)
    # TODO: location info.  Use the = token?
    #lval.spids.append(spid)
    return lval

  elif node.tag == sh_lhs_expr_e.IndexedName:  # (( a[42] = 42 ))
    # The index of MaybeStrArray needs to be coerced to int, but not the index of
    # an AssocArray.
    if mem.IsAssocArray(node.name, scope_e.Dynamic):
      key = arith_ev.EvalWordToString(node.index)
      lval = lvalue.Keyed(node.name, key)
    else:
      index = arith_ev.EvalToInt(node.index)
      lval = lvalue.Indexed(node.name, index)
      # TODO: location info.  Use the = token?
      #lval.spids.append(node.spids[0])

  else:
    raise AssertionError(node.tag)

  return lval


def EvalLhsAndLookup(node, arith_ev, mem, exec_opts,
                     lookup_mode=scope_e.Dynamic):
  # type: (sh_lhs_expr_t, ArithEvaluator, Mem, ExecOpts, scope_t) -> Tuple[value_t, lvalue_t]
  """Evaluate the operand for i++, a[0]++, i+=2, a[0]+=2 as an R-value.

  Also used by the Executor for s+='x' and a[42]+='x'.

  Args:
    node: syntax_asdl.sh_lhs_expr

  Returns:
    value_t, lvalue_t
  """
  #log('sh_lhs_expr NODE %s', node)

  assert isinstance(node, sh_lhs_expr_t), node

  if node.tag == sh_lhs_expr_e.Name:  # a = b
    # Problem: It can't be an array?
    # a=(1 2)
    # (( a++ ))
    lval = lvalue.Named(node.name)
    val = _LookupVar(node.name, mem, exec_opts)

  elif node.tag == sh_lhs_expr_e.IndexedName:  # a[1] = b
    # See tdop.IsIndexable for valid values:
    # - VarRef (not Name): a[1]
    # - FuncCall: f(x), 1
    # - Binary LBracket: f[1][1] -- no semantics for this?

    val = mem.GetVar(node.name)

    if val.tag == value_e.Str:
      e_die("Can't assign to characters of string %r", node.name)

    elif val.tag == value_e.Undef:
      # compatible behavior: Treat it like an array.
      # TODO: Does this code ever get triggered?  It seems like the error is
      # caught earlier.

      index = arith_ev.EvalToInt(node.index)
      lval = lvalue.Indexed(node.name, index)
      if exec_opts.nounset:
        e_die("Undefined variable can't be indexed")
      else:
        val = value.Str('')

    elif val.tag == value_e.MaybeStrArray:

      #log('ARRAY %s -> %s, index %d', node.name, array, index)
      array = val.strs
      index = arith_ev.EvalToInt(node.index)
      lval = lvalue.Indexed(node.name, index)
      # NOTE: Similar logic in RHS Arith_LBracket
      try:
        s = array[index]
      except IndexError:
        s = None

      if s is None:
        val = value.Str('')  # NOTE: Other logic is value.Undef()?  0?
      else:
        assert isinstance(s, str), s
        val = value.Str(s)

    elif val.tag == value_e.AssocArray:  # declare -A a; a['x']+=1
      key = arith_ev.EvalWordToString(node.index)
      lval = lvalue.Keyed(node.name, key)

      s = val.d.get(key)
      if s is None:
        val = value.Str('')
      else:
        val = value.Str(s)

    else:
      raise AssertionError(val.tag)

  else:
    raise AssertionError(node.tag)

  return val, lval


class _ExprEvaluator(object):
  """Shared between arith and bool evaluators.

  They both:

  1. Convert strings to integers, respecting shopt -s strict_arith.
  2. Look up variables and evaluate words.
  """

  def __init__(self, mem, exec_opts, word_ev, errfmt):
    # type: (Mem, Any, word_eval.SimpleWordEvaluator, ErrorFormatter) -> None
    # TODO: Remove Any by fixing _DummyExecOpts in osh/builtin_bracket.py
    self.mem = mem
    self.exec_opts = exec_opts
    self.word_ev = word_ev
    self.errfmt = errfmt

  def _StringToIntegerOrError(self, s, blame_word=None,
                              span_id=runtime.NO_SPID):
    """Used by both [[ $x -gt 3 ]] and (( $x ))."""
    if span_id == runtime.NO_SPID and blame_word:
      span_id = word_.LeftMostSpanForWord(blame_word)

    try:
      i = _StringToInteger(s, span_id=span_id)
    except error.FatalRuntime as e:
      if self.exec_opts.strict_arith:
        raise
      else:
        self.errfmt.PrettyPrintError(e, prefix='warning: ')
        i = 0
    return i


class ArithEvaluator(_ExprEvaluator):

  def _ValToIntOrError(self, val, blame_word=None, span_id=runtime.NO_SPID):
    # type: (value_t, word_t, int) -> int
    if span_id == runtime.NO_SPID and blame_word:
      span_id = word_.LeftMostSpanForWord(blame_word)
    #log('_ValToIntOrError span=%s blame=%s', span_id, blame_word)

    try:
      if val.tag == value_e.Undef:  # 'nounset' already handled before got here
        # Happens upon a[undefined]=42, which unfortunately turns into a[0]=42.
        #log('blame_word %s   arena %s', blame_word, self.arena)
        e_die('Undefined value in arithmetic context', span_id=span_id)

      if val.tag == value_e.Int:
        return val.i

      if val.tag == value_e.Str:
        return _StringToInteger(val.s, span_id=span_id)  # calls e_die

    except error.FatalRuntime as e:
      if self.exec_opts.strict_arith:
        raise
      else:
        span_id = word_.SpanIdFromError(e)
        self.errfmt.PrettyPrintError(e, prefix='warning: ')
        return 0

    # Arrays and associative arrays always fail -- not controlled by strict_arith.
    # In bash, (( a )) is like (( a[0] )), but I don't want that.
    # And returning '0' gives different results.
    e_die("Expected a value convertible to integer, got %s", val,
          span_id=span_id)

  def _EvalLhsAndLookupArith(self, node):
    """
    Args:
      node: sh_lhs_expr

    Returns:
      (Python object, lvalue_t)
    """
    val, lval = EvalLhsAndLookup(node, self, self.mem, self.exec_opts)

    if val.tag == value_e.MaybeStrArray:
      e_die("Can't use assignment like ++ or += on arrays")

    # TODO: attribute a span ID here.  There are a few cases, like UnaryAssign
    # and BinaryAssign.
    span_id = word_.SpanForLhsExpr(node)
    i = self._ValToIntOrError(val, span_id=span_id)
    return i, lval

  def _Store(self, lval, new_int):
    val = value.Str(str(new_int))
    self.mem.SetVar(lval, val, (), scope_e.Dynamic)

  def EvalToInt(self, node):
    # type: (arith_expr_t) -> int
    """Used externally by ${a[i+1]} and ${a:start:len}.

    Also used internally.
    """
    val = self.Eval(node)
    i = self._ValToIntOrError(val)
    return i

  def Eval(self, node):
    # type: (arith_expr_t) -> value_t
    """
    Args:
      node: arith_expr_t

    Returns:
      None for Undef  (e.g. empty cell)  TODO: Don't return 0!
      int for Str
      List[int] for MaybeStrArray
      Dict[str, str] for AssocArray (TODO: Should we support this?)

    NOTE: (( A['x'] = 'x' )) and (( x = A['x'] )) are syntactically valid in
    bash, but don't do what you'd think.  'x' sometimes a variable name and
    sometimes a key.
    """
    # OSH semantics: Variable NAMES cannot be formed dynamically; but INTEGERS
    # can.  ${foo:-3}4 is OK.  $? will be a compound word too, so we don't have
    # to handle that as a special case.

    if node.tag == arith_expr_e.VarRef:  # $(( x ))  (can be array)
      tok = node.token
      return _LookupVar(tok.val, self.mem, self.exec_opts)

    if node.tag == arith_expr_e.ArithWord:  # $(( $x )) $(( ${x}${y} )), etc.
      return self.word_ev.EvalWordToString(node.w)

    if node.tag == arith_expr_e.UnaryAssign:  # a++
      op_id = node.op_id
      old_int, lval = self._EvalLhsAndLookupArith(node.child)

      if op_id == Id.Node_PostDPlus:  # post-increment
        new_int = old_int + 1
        ret = old_int

      elif op_id == Id.Node_PostDMinus:  # post-decrement
        new_int = old_int - 1
        ret = old_int

      elif op_id == Id.Arith_DPlus:  # pre-increment
        new_int = old_int + 1
        ret = new_int

      elif op_id == Id.Arith_DMinus:  # pre-decrement
        new_int = old_int - 1
        ret = new_int

      else:
        raise AssertionError(op_id)

      #log('old %d new %d ret %d', old_int, new_int, ret)
      self._Store(lval, new_int)
      return value.Int(ret)

    if node.tag == arith_expr_e.BinaryAssign:  # a=1, a+=5, a[1]+=5
      op_id = node.op_id

      if op_id == Id.Arith_Equal:
        lval = _EvalLhsArith(node.left, self.mem, self)
        # Disallowing (( a = myarray ))
        # It has to be an integer
        rhs_int = self.EvalToInt(node.right)
        self._Store(lval, rhs_int)
        return value.Int(rhs_int)

      old_int, lval = self._EvalLhsAndLookupArith(node.left)
      rhs = self.EvalToInt(node.right)

      if op_id == Id.Arith_PlusEqual:
        new_int = old_int + rhs
      elif op_id == Id.Arith_MinusEqual:
        new_int = old_int - rhs
      elif op_id == Id.Arith_StarEqual:
        new_int = old_int * rhs
      elif op_id == Id.Arith_SlashEqual:
        try:
          new_int = old_int / rhs
        except ZeroDivisionError:
          # TODO: location
          e_die('Divide by zero')
      elif op_id == Id.Arith_PercentEqual:
        new_int = old_int % rhs

      elif op_id == Id.Arith_DGreatEqual:
        new_int = old_int >> rhs
      elif op_id == Id.Arith_DLessEqual:
        new_int = old_int << rhs
      elif op_id == Id.Arith_AmpEqual:
        new_int = old_int & rhs
      elif op_id == Id.Arith_PipeEqual:
        new_int = old_int | rhs
      elif op_id == Id.Arith_CaretEqual:
        new_int = old_int ^ rhs
      else:
        raise AssertionError(op_id)  # shouldn't get here

      self._Store(lval, new_int)
      return value.Int(new_int)

    if node.tag == arith_expr_e.Unary:
      op_id = node.op_id

      i = self.EvalToInt(node.child)

      if op_id == Id.Node_UnaryPlus:
        ret = i
      elif op_id == Id.Node_UnaryMinus:
        ret = -i

      elif op_id == Id.Arith_Bang:  # logical negation
        ret = 1 if i == 0 else 0
      elif op_id == Id.Arith_Tilde:  # bitwise complement
        ret = ~i
      else:
        raise AssertionError(op_id)  # shouldn't get here

      return value.Int(ret)

    if node.tag == arith_expr_e.Binary:
      op_id = node.op_id

      # Short-circuit evaluation for || and &&.
      if op_id == Id.Arith_DPipe:
        lhs = self.EvalToInt(node.left)
        if lhs == 0:
          rhs = self.EvalToInt(node.right)
          ret = int(rhs != 0)
        else:
          ret = 1  # true
        return value.Int(ret)

      if op_id == Id.Arith_DAmp:
        lhs = self.EvalToInt(node.left)
        if lhs == 0:
          ret = 0  # false
        else:
          rhs = self.EvalToInt(node.right)
          ret = int(rhs != 0)
        return value.Int(ret)

      if op_id == Id.Arith_LBracket:
        # NOTE: Similar to bracket_op_e.ArrayIndex in osh/word_eval.py

        lhs = self.Eval(node.left)
        UP_lhs = lhs
        with tagswitch(lhs) as case:
          if case(value_e.MaybeStrArray):
            lhs = cast(value__MaybeStrArray, UP_lhs)
            rhs_int = self.EvalToInt(node.right)
            try:
              # could be None because representation is sparse
              s = lhs.strs[rhs_int]
            except IndexError:
              s = None

          elif case(value_e.AssocArray):
            lhs = cast(value__AssocArray, UP_lhs)
            key = self.arith_ev.EvalWordToString(node.right)
            s = lhs.d.get(key)

          else:
            # TODO: Add error context
            e_die('Expected array or assoc in index expression, got %s', lhs)

        if s is None:
          val = value.Undef()
        else:
          val = value.Str(s)

        return val

      if op_id == Id.Arith_Comma:
        self.Eval(node.left)  # throw away result
        return self.Eval(node.right)

      # Rest are integers
      lhs = self.EvalToInt(node.left)
      rhs = self.EvalToInt(node.right)

      if op_id == Id.Arith_Plus:
        ret = lhs + rhs
      elif op_id == Id.Arith_Minus:
        ret = lhs - rhs
      elif op_id == Id.Arith_Star:
        ret =  lhs * rhs
      elif op_id == Id.Arith_Slash:
        try:
          ret = lhs / rhs
        except ZeroDivisionError:
          # TODO: _ErrorWithLocation should also accept arith_expr ?  I
          # think I needed that for other stuff.
          # Or I could blame the '/' token, instead of op_id.
          error_expr = node.right  # node is Binary
          if error_expr.tag == arith_expr_e.VarRef:
            # TODO: VarRef should store a token instead of a string!
            e_die('Divide by zero (name)')
          elif error_expr.tag == arith_expr_e.ArithWord:
            e_die('Divide by zero', word=node.right.w)
          else:
            e_die('Divide by zero')

      elif op_id == Id.Arith_Percent:
        ret= lhs % rhs

      elif op_id == Id.Arith_DStar:
        # OVM is stripped of certain functions that are somehow necessary for
        # exponentiation.
        # Python/ovm_stub_pystrtod.c:21: PyOS_double_to_string: Assertion `0'
        # failed.
        if rhs < 0:
          e_die("Exponent can't be less than zero")  # TODO: error location
        ret = 1
        for i in xrange(rhs):
          ret *= lhs

      elif op_id == Id.Arith_DEqual:
        ret = int(lhs == rhs)
      elif op_id == Id.Arith_NEqual:
        ret = int(lhs != rhs)
      elif op_id == Id.Arith_Great:
        ret = int(lhs > rhs)
      elif op_id == Id.Arith_GreatEqual:
        ret = int(lhs >= rhs)
      elif op_id == Id.Arith_Less:
        ret = int(lhs < rhs)
      elif op_id == Id.Arith_LessEqual:
        ret = int(lhs <= rhs)

      elif op_id == Id.Arith_Pipe:
        ret = lhs | rhs
      elif op_id == Id.Arith_Amp:
        ret = lhs & rhs
      elif op_id == Id.Arith_Caret:
        ret = lhs ^ rhs

      # Note: how to define shift of negative numbers?
      elif op_id == Id.Arith_DLess:
        ret = lhs << rhs
      elif op_id == Id.Arith_DGreat:
        ret = lhs >> rhs
      else:
        raise AssertionError(op_id)

      return value.Int(ret)

    if node.tag == arith_expr_e.TernaryOp:
      cond = self.EvalToInt(node.cond)
      if cond:  # nonzero
        return self.Eval(node.true_expr)
      else:
        return self.Eval(node.false_expr)

    raise AssertionError("Unhandled node %r" % node.__class__.__name__)

  def EvalWordToString(self, node):
    # type: (arith_expr_t) -> str
    """
    Args:
      node: arith_expr_t

    Returns:
      str

    Raises:
      error.FatalRuntime if the expression isn't a string
      Or if it contains a bare variable like a[x]

    These are allowed because they're unambiguous, unlike a[x]

    a[$x] a["$x"] a["x"] a['x']
    """
    if node.tag != arith_expr_e.ArithWord:  # $(( $x )) $(( ${x}${y} )), etc.
      # TODO: location info for orginal
      e_die("Associative array keys must be strings: $x 'x' \"$x\" etc.")

    val = self.word_ev.EvalWordToString(node.w)
    return val.s


class BoolEvaluator(_ExprEvaluator):

  def _SetRegexMatches(self, matches):
    """For ~= to set the BASH_REMATCH array."""
    state.SetGlobalArray(self.mem, 'BASH_REMATCH', matches)

  def _EvalCompoundWord(self, word, quote_kind=quote_e.Default):
    # type: (compound_word, quote_t) -> str
    val = self.word_ev.EvalWordToString(word, quote_kind=quote_kind)
    return val.s

  def Eval(self, node):
    # type: (bool_expr_t) -> bool
    #print('!!', node.tag)

    if node.tag == bool_expr_e.WordTest:
      s = self._EvalCompoundWord(node.w)
      return bool(s)

    if node.tag == bool_expr_e.LogicalNot:
      b = self.Eval(node.child)
      return not b

    if node.tag == bool_expr_e.LogicalAnd:
      # Short-circuit evaluation
      if self.Eval(node.left):
        return self.Eval(node.right)
      else:
        return False

    if node.tag == bool_expr_e.LogicalOr:
      if self.Eval(node.left):
        return True
      else:
        return self.Eval(node.right)

    if node.tag == bool_expr_e.Unary:
      op_id = node.op_id
      s = self._EvalCompoundWord(node.child)

      # Now dispatch on arg type
      arg_type = BOOL_ARG_TYPES[op_id]  # could be static in the LST?

      if arg_type == bool_arg_type_e.Path:
        # Only use lstat if we're testing for a symlink.
        if op_id in (Id.BoolUnary_h, Id.BoolUnary_L):
          try:
            mode = posix.lstat(s).st_mode
          except OSError:
            # TODO: simple_test_builtin should this as status=2.
            #e_die("lstat() error: %s", e, word=node.child)
            return False

          return stat.S_ISLNK(mode)

        try:
          st = posix.stat(s)
        except OSError as e:
          # TODO: simple_test_builtin should this as status=2.
          # Problem: we really need errno, because test -f / is bad argument,
          # while test -f /nonexistent is a good argument but failed.  Gah.
          # ENOENT vs. ENAMETOOLONG.
          #e_die("stat() error: %s", e, word=node.child)
          return False
        mode = st.st_mode

        if op_id in (Id.BoolUnary_e, Id.BoolUnary_a):  # -a is alias for -e
          return True

        if op_id == Id.BoolUnary_f:
          return stat.S_ISREG(mode)

        if op_id == Id.BoolUnary_d:
          return stat.S_ISDIR(mode)

        if op_id == Id.BoolUnary_b:
          return stat.S_ISBLK(mode)

        if op_id == Id.BoolUnary_c:
          return stat.S_ISCHR(mode)

        if op_id == Id.BoolUnary_p:
          return stat.S_ISFIFO(mode)

        if op_id == Id.BoolUnary_S:
          return stat.S_ISSOCK(mode)

        if op_id == Id.BoolUnary_x:
          return posix.access(s, posix.X_OK)

        if op_id == Id.BoolUnary_r:
          return posix.access(s, posix.R_OK)

        if op_id == Id.BoolUnary_w:
          return posix.access(s, posix.W_OK)

        if op_id == Id.BoolUnary_s:
          return st.st_size != 0

        if op_id == Id.BoolUnary_O:
          return st.st_uid == posix.geteuid()

        if op_id == Id.BoolUnary_G:
          return st.st_gid == posix.getegid()

        e_die("%s isn't implemented", op_id)  # implicit location

      if arg_type == bool_arg_type_e.Str:
        if op_id == Id.BoolUnary_z:
          return not bool(s)
        if op_id == Id.BoolUnary_n:
          return bool(s)

        raise AssertionError(op_id)  # should never happen

      if arg_type == bool_arg_type_e.Other:
        if op_id == Id.BoolUnary_t:
          try:
            fd = int(s)
          except ValueError:
            # TODO: Need location information of [
            e_die('Invalid file descriptor %r', s, word=node.child)
          try:
            return posix.isatty(fd)
          # fd is user input, and causes this exception in the binding.
          except OverflowError:
            e_die('File descriptor %r is too big', s, word=node.child)

        # See whether 'set -o' options have been set
        if op_id == Id.BoolUnary_o:
          b = getattr(self.exec_opts, s, None)
          return False if b is None else b

        e_die("%s isn't implemented", op_id)  # implicit location

      raise AssertionError(arg_type)  # should never happen

    if node.tag == bool_expr_e.Binary:
      op_id = node.op_id

      s1 = self._EvalCompoundWord(node.left)
      # Whether to glob escape
      with switch(op_id) as case:
        if case(Id.BoolBinary_GlobEqual, Id.BoolBinary_GlobDEqual,
                Id.BoolBinary_GlobNEqual):
          quote_kind = quote_e.FnMatch
        elif case(Id.BoolBinary_EqualTilde):
          quote_kind = quote_e.ERE
        else:
          quote_kind = quote_e.Default

      s2 = self._EvalCompoundWord(node.right, quote_kind=quote_kind)

      # Now dispatch on arg type
      arg_type = BOOL_ARG_TYPES[op_id]

      if arg_type == bool_arg_type_e.Path:
        try:
          st1 = posix.stat(s1)
        except OSError:
          st1 = None
        try:
          st2 = posix.stat(s2)
        except OSError:
          st2 = None

        if op_id in (Id.BoolBinary_nt, Id.BoolBinary_ot):
          # pretend it's a very old file
          m1 = 0 if st1 is None else st1.st_mtime
          m2 = 0 if st2 is None else st2.st_mtime
          if op_id == Id.BoolBinary_nt:
            return m1 > m2
          else:
            return m1 < m2

        if op_id == Id.BoolBinary_ef:
          if st1 is None:
            return False
          if st2 is None:
            return False
          return st1.st_dev == st2.st_dev and st1.st_ino == st2.st_ino

        raise AssertionError(op_id)

      if arg_type == bool_arg_type_e.Int:
        # NOTE: We assume they are constants like [[ 3 -eq 3 ]].
        # Bash also allows [[ 1+2 -eq 3 ]].
        i1 = self._StringToIntegerOrError(s1, blame_word=node.left)
        i2 = self._StringToIntegerOrError(s2, blame_word=node.right)

        if op_id == Id.BoolBinary_eq:
          return i1 == i2
        if op_id == Id.BoolBinary_ne:
          return i1 != i2
        if op_id == Id.BoolBinary_gt:
          return i1 > i2
        if op_id == Id.BoolBinary_ge:
          return i1 >= i2
        if op_id == Id.BoolBinary_lt:
          return i1 < i2
        if op_id == Id.BoolBinary_le:
          return i1 <= i2

        raise AssertionError(op_id)  # should never happen

      if arg_type == bool_arg_type_e.Str:

        if op_id in (Id.BoolBinary_GlobEqual, Id.BoolBinary_GlobDEqual):
          #log('Matching %s against pattern %s', s1, s2)
          return libc.fnmatch(s2, s1)

        if op_id == Id.BoolBinary_GlobNEqual:
          return not libc.fnmatch(s2, s1)

        if op_id in (Id.BoolBinary_Equal, Id.BoolBinary_DEqual):
          return s1 == s2

        if op_id == Id.BoolBinary_NEqual:
          return s1 != s2

        if op_id == Id.BoolBinary_EqualTilde:
          # TODO: This should go to --debug-file
          #log('Matching %r against regex %r', s1, s2)
          try:
            matches = libc.regex_match(s2, s1)
          except RuntimeError:
            # Status 2 indicates a regex parse error.  This is fatal in OSH but
            # not in bash, which treats [[ like a command with an exit code.
            e_die("Invalid regex %r", s2, word=node.right, status=2)

          if matches is None:
            return False

          self._SetRegexMatches(matches)
          return True

        if op_id == Id.Op_Less:
          return s1 < s2

        if op_id == Id.Op_Great:
          return s1 > s2

        raise AssertionError(op_id)  # should never happen

    raise AssertionError(node.tag)
