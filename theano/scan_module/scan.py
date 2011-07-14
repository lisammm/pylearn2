"""
This module provides the Scan Op

Scanning is a general form of recurrence, which can be used for looping.
The idea is that you *scan* a function along some input sequence, producing
an output at each time-step that can be seen (but not modified) by the
function at the next time-step. (Technically, the function can see the
previous K  time-steps of your outputs and L time steps (from the past and
future) of your inputs.

So for example, ``sum()`` could be computed by scanning the ``z+x_i``
function over a list, given an initial state of ``z=0``.

Special cases:

* A *reduce* operation can be performed by returning only the last
  output of a ``scan``.
* A *map* operation can be performed by applying a function that
  ignores previous steps of the outputs.

Often a for-loop can be expressed as a ``scan()`` operation, and ``scan`` is
the closest that theano comes to looping. The advantage of using ``scan``
over for loops is that it allows the number of iterations to be a part of
the symbolic graph.

The Scan Op should typically be used by calling any of the following
functions: ``scan()``, ``map()``, ``reduce()``, ``foldl()``,
``foldr()``.
"""
__docformat__ = 'restructedtext en'
__authors__ = ( "Razvan Pascanu "
                "Frederic Bastien "
                "James Bergstra "
                "Pascal Lamblin "  )
__copyright__ = "(c) 2010, Universite de Montreal"
__contact__ = "Razvan Pascanu <r.pascanu@gmail>"


import itertools
import logging
import numpy
import warnings

from theano.compile import SharedVariable, function
from theano import compile
from theano import gof
from theano.tensor import opt
from theano import tensor
from theano import config

import scan_op
from scan_op import safe_new, safe_to_cpu
import scan_utils
from scan_utils import safe_new, safe_to_cpu, traverse
from theano.sandbox import cuda
from theano.updates import Updates

# Logging function for sending warning or info
_logger = logging.getLogger('theano.scan_module.scan')

def warning(*msg):
    _logger.warning('WARNING theano.scan: '+' '.join(msg))

def info(*msg):
    _logger.info('INFO theano.scan: '+' '.join(msg))


def scan( fn
         , sequences         = None
         , outputs_info      = None
         , non_sequences     = None
         , n_steps           = None
         , truncate_gradient = -1
         , go_backwards      = False
         , mode              = None
         , name              = None ):
    """
    This function constructs and applies a Scan op to the provided
    arguments.

    :param fn:
        ``fn`` is a function that describes the operations involved in one
        step of ``scan``. ``fn`` should construct variables describing the
        output of one iteration step. It should expect as input theano
        variables representing all the time slices of the input sequences
        and outputs, and all other arguments given to scan as
        ``non_sequences``. The order in which scan passes this variables
        to ``fn``  is the following :

        * all time slices of the first sequence
        * all time slices of the second sequence
        * ...
        * all time slices of the last sequence
        * all time slices of the first output
        * all time slices of the second otuput
        * ...
        * all time slices of the last output
        * all other arguments (the list given as `non_sequences` to
            scan)

        The order of the sequences is the same as the one in the list
        `sequences` given to scan. The order of the outputs is the sane
        as the order of ``output_info``. For any sequence or output the
        order of the time slices is the same as the order of the time
        taps provided. For example if one writes the following :

        .. code-block:: python

            scan(fn, sequences = [ dict(input= Sequence1, taps = [-3,2,-1])
                                 , Sequence2
                                 , dict(input =  Sequence3, taps = 3) ]
                   , outputs_info = [ dict(initial =  Output1, taps = [-3,-5])
                                    , dict(initial = Output2, taps = None)
                                    , Output3 ]
                   , non_sequences = [ Argument1, Argument 2])

        ``fn`` should expect the following arguments in this given order:

        #. ``Sequence1[t-3]``
        #. ``Sequence1[t+2]``
        #. ``Sequence1[t-1]``
        #. ``Sequence2[t]``
        #. ``Sequence3[t+3]``
        #. ``Output1[t-3]``
        #. ``Output1[t-5]``
        #. ``Output3[t-1]``
        #. ``Argument1``
        #. ``Argument2``

        The list of ``non_sequences`` can also contain shared variables
        used in the function, though ``scan`` is able to figure those
        out on its own so they can be skipped. For the clarity of the
        code we recommand though to provide them to scan.

        The function is expected to return two things. One is a list of
        outputs ordered in the same order as ``outputs_info``, with the
        difference that there should be only one output variable per
        output initial state (even if no tap value is used). Secondly
        `fn` should return an update dictionary ( that tells how to
        update any shared variable after each iteration ste). The
        dictionary can optionally be given as a list of tuples. There is
        no constraint on the order of these two list, ``fn`` can return
        either ``(outputs_list, update_dictionary)`` or
        ``(update_dictionary, outputs_list)`` or just one of the two (in
        case the other is empty).


    :param sequences:
        ``sequences`` is the list of Theano variables or dictionaries
        describing the sequences ``scan`` has to iterate over. If a
        sequence is given as wrapped in a dictionary a set of optional
        information can be provided about the sequence. The dictionary
        should have the following keys:

        * ``input`` (*mandatory*) -- Theano variable representing the
          sequence.

        * ``taps`` -- Temporal taps of the sequence required by ``fn``.
          They are provided as a list of integers, where a value ``k``
          impiles that at iteration step ``t`` scan will pass to ``fn``
          the slice ``t+k``. Default value is ``[0]``

        Any Theano variable in the list ``sequences`` is automatically
        wrapped into a dictionary where ``taps`` is set to ``[0]``


    :param outputs_info:
        ``outputs_info`` is the list of Theano variables or dictionaries
        describing the initial state of the outputs computed
        recurrently. When this initial states are given as dictionary
        optional information can be provided about the output corresponding
        to these initial states. The dictionary should have the following
        keys:

        * ``initial`` -- Theano variable that represents the initial
          state of a given output. In case the output is not computed
          recursively (think of a map) and does not require a initial
          state this field can be skiped. Given that only the previous
          time step of the output is used by ``fn`` the initial state
          should have the same shape as the output. If multiple time
          taps are used, the initial state should have one extra
          dimension that should cover all the possible taps. For example
          if we use ``-5``, ``-2`` and ``-1`` as past taps, at step 0,
          ``fn`` will require (by an abuse of notation) ``output[-5]``,
          ``output[-2]`` and ``output[-1]``. This will be given by
          the initial state, which in this case should have the shape
          (5,)+output.shape. If this variable containing the initial
          state is called ``init_y`` then ``init_y[0]`` *corresponds to*
          ``output[-5]``. ``init_y[1]`` *correponds to* ``output[-4]``,
          ``init_y[2]`` corresponds to ``output[-3]``, ``init_y[3]``
          coresponds to ``output[-2]``, ``init_y[4]`` corresponds to
          ``output[-1]``. While this order might seem strange, it comes
          natural from splitting an array at a given point. Assume that
          we have a array ``x``, and we choose ``k`` to be time step
          ``0``. Then our initial state would be ``x[:k]``, while the
          output will be ``x[k:]``. Looking at this split, elements in
          ``x[:k]`` are ordered exactly like those in ``init_y``.
        * ``taps`` -- Temporal taps of the output that will be pass to
          ``fn``. They are provided as a list of *negative* integers,
          where a value ``k`` implies that at iteration step ``t`` scan
          will pass to ``fn`` the slice ``t+k``.
        * ``return_steps`` -- Integer representing the number of steps
          to return for the current steps. For example, if ``k`` is
          provided, ``scan`` will return ``output[-k:]``. This is meant
          as a hint, based on ``k`` and the past taps of the outputs used,
          scan can be smart about the amount of memory it requires to
          store intermidiate results. If not given, or ``0``, ``scan``
          will return all computed steps.

        ``scan`` will follow this logic if partial information is given:

        * If an output is not wrapped in a dictionary, ``scan`` will wrap
          it in one assuming that you use only the last step of the output
          (i.e. it makes your tap value list equal to [-1]).
        * If you wrap an output in a dictionary and you do not provide any
          taps but you provide an initial state it will assume that you are
          using only a tap value of -1.
        * If you wrap an output in a dictionary but you do not provide any
          initial state, it assumes that you are not using any form of
          taps.
        * If you provide a ``None`` instead of a variable or a dictionary
          ``scan`` assumes that you will not use any taps for this output
          (like for example in case of a map)

        If ``outputs_info`` is an empty list or None, ``scan`` assumes
        that no tap is used for any of the otuputs. If information is
        provided just for a subset of the outputs an exception is
        raised (because there is no convention on how scan should map
        the provided information to the outputs of ``fn``)


    :param non_sequences:
        ``non_sequences`` is the list of arguments that are passed to
        ``fn`` at each steps. Once can opt to exclude shared variables
        used in ``fn`` from this list.


    :param n_steps:
        ``n_steps`` is the number of steps to iterate given as an int
        or Theano scalar. If any of the input sequences do not have
        enough elements, scan will raise an error. If the *value is 0* the
        outputs will have *0 rows*. If the value is negative, ``scan``
        run backwards in time. If the ``go_backwards`` flag is already
        set and also ``n_steps`` is negative, ``scan`` will run forward
        in time. If n stpes is not provided, or is a constant that
        evaluates to ``None``, ``inf`` or ``NaN``, ``scan`` will figure
        out the amount of steps it should run given its input sequences.


    :param truncate_gradient:
        ``truncate_gradient`` is the number of steps to use in truncated
        BPTT.  If you compute gradients through a scan op, they are
        computed using backpropagation through time. By providing a
        different value then -1, you choose to use truncated BPTT instead
        of classical BPTT, where you go for only ``truncate_gradient``
        number of steps back in time.


    :param go_backwards:
        ``go_backwards`` is a flag indicating if ``scan`` should go
        backwards through the sequences. If you think of each sequence
        as indexed by time, making this flag True would mean that
        ``scan`` goes back in time, namely that for any sequence it
        starts from the end and goes towards 0.


    :param name:
        When profiling ``scan`` it is crucial to provide a name for any
        instance of ``scan``. The profiler will produce an overall
        profile of your code as well as profiles for doing one iteration
        step for each instance of ``scan``. The ``name`` of the instance is
        how you differentiate between all these profiles.


    :param mode:
        It is recommended to leave this argument to None, especially
        when profiling ``scan`` (otherwise the results are not going to
        be accurate). If you prefer the computations of one step os
        ``scan`` to be done differently then the entire function set
        this parameters (see ``theano.function`` for details about
        possible values and their meaning).


    :rtype: tuple
    :return: tuple of the form (outputs, updates); ``outputs`` is either a
             Theano variable or a list of Theano variables representing the
             outputs of ``scan`` (in the same order as in
             ``outputs_info``). ``updates`` is a dictionary specifying the
             update rules for all shared variables used in the scan
             operation. This dictionary should be passed to
             ``theano.function`` when you compile your function.
    """
    # General observation : this code is executed only once, at creation
    # of the computational graph, so we don't yet need to be smart about
    # anything (to speed things up)

    ##
    ###   Step 1. Wrap all inputs in dictionaries and add default values
    ##

    # check if inputs are just single variables instead of lists
    def wrap_into_list(x):
        '''
        Wrap the input into a list if it is not already a list
        '''
        if x is None:
            return []
        elif not isinstance(x, (list,tuple)):
            return [x]
        else:
            return list(x)

    seqs      = wrap_into_list(sequences)
    outs_info = wrap_into_list(outputs_info)
    non_seqs  = wrap_into_list(non_sequences)


    # If we provided a known number of steps ( before compilation)
    # and if that number is 1 or -1, then we can skip the Scan Op,
    # and just apply the inner function once
    # To do that we check here to see the nature of n_steps
    n_fixed_steps = None

    if isinstance( n_steps, (float,int)):
        n_fixed_steps = int(n_steps)
    else:
        try :
            n_fixed_steps = opt.get_constant_value(n_steps)
        except:
            n_fixed_steps = None

    # Check n_steps is an int
    if ( hasattr(n_steps,'dtype') and
        str(n_steps.dtype)[:3] not in ('uin','int') ):
        raise ValueError(' n_steps must be an int. dtype provided '
                         'is %s'%n_steps.dtype)

    # compute number of sequences and number of outputs
    n_seqs = len(seqs)
    n_outs = len(outs_info)

    return_steps = {}
    # wrap sequences in a dictionary if they are not already dictionaries
    for i in xrange(n_seqs):
        if not isinstance(seqs[i], dict) :
            seqs[i] = dict(input=seqs[i], taps=[0])
        elif seqs[i].get('taps',None):
            seqs[i]['taps'] = wrap_into_list(seqs[i]['taps'])
        elif seqs[i].get('taps',True) is None:
            # seqs dictionary does not have the ``taps`` key
            seqs[i]['taps'] = [0]

    # wrap outputs info in a dictionary if they are not already in one
    for i in xrange(n_outs):
        if outs_info[i]:
            if isinstance(outs_info[i], dict):
                if outs_info[i].get('return_steps', None):
                    return_steps[i] = outs_info[i]['return_steps']

            if not isinstance(outs_info[i], dict):
                # by default any output has a tap value of -1
                outs_info[i] = dict(initial=outs_info[i], taps = [-1])
            elif (not outs_info[i].get('initial',None) and
                    outs_info[i].get('taps',None)):
                # ^ no initial state but taps provided
                raise ValueError( ( 'If you are using slices of an output '
                                    'you need to provide a initial state '
                                   'for it'), outs_info[i] )
            elif (outs_info[i].get('initial',None) and
                  not outs_info[i].get('taps',None)):
                # ^ initial state but taps not provided
                if outs_info[i].has_key('taps'):
                    # ^ explicitly provided a None for taps
                    warning (' Output %s ( index %d) has a initial state '
                             ' but taps is explicitly set to None ' % (
                                 getattr(outs_info[i]['initial'],'name','None')
                                 , i) )
                outs_info[i]['taps'] = [-1]
        else:
            # if a None is provided as the output info we replace it
            # with an empty dict() to simplify handling
            outs_info[i] = dict()

    ##
    ###   Step 2. Generate inputs and outputs of the inner functions
    ###           for compiling a dummy function (Iteration #1)
    ##

    # create theano inputs for the recursive function
    # note : this is a first batch of possible inputs that will
    #        be compiled in a dummy function; we used this dummy
    #        function to detect shared variables and their updates
    #        and to construct a new and complete list of inputs and
    #        outputs

    n_seqs       =  0
    scan_seqs    = [] # Variables passed as inputs to the scan op
    inner_seqs   = [] # Variables passed as inputs to the inner function
    inner_slices = [] # Actual slices if scan is removed from the picture
    # go through sequences picking up time slices as needed
    for i,seq in enumerate(seqs):
        # Note that you can have something like no taps for
        # a sequence, though is highly unlikely in practice
        if 'taps' in seq:
            # go through the indicated slice
            mintap = numpy.min(seq['taps'])
            maxtap = numpy.max(seq['taps'])
            for k in seq['taps']:
                # create one slice of the input
                # Later on, if we decide not to use scan because we are
                # going for just one step, it makes things easier if we
                # compute the correct outputs here. This way we can use
                # the output of the lambda expression directly to replace
                # the output of scan.

                # If not we need to use copies, that will be replaced at
                # each frame by the corresponding slice
                actual_slice = seq['input'][k-mintap]
                _seq_val = tensor.as_tensor_variable(seq['input'])
                _seq_val_slice = _seq_val[k-mintap]
                nw_slice = _seq_val_slice.type()

                # Try to transfer test_value to the new variable
                if config.compute_test_value != 'off':
                    try:
                        nw_slice.tag.test_value = gof.Op._get_test_value(_seq_val_slice)
                    except AttributeError, e:
                        if config.compute_test_value != 'ignore':
                            # No need to print a warning or raise an error now,
                            # it will be done when fn will be called.
                            info(('Cannot compute test value for the inner '
                                'function of scan, input value missing'), e)

                # Add names to slices for debugging and pretty printing ..
                # that is if the input already has a name
                if getattr(seq['input'],'name', None) is not None:
                    if k > 0:
                        nw_name = seq['input'].name + '[t+%d]'%k
                    elif k == 0:
                        nw_name = seq['input'].name + '[t]'
                    else:
                        nw_name = seq['input'].name + '[t%d]'%k
                    nw_slice.name = nw_name

                # We cut the sequence such that seq[i] to correspond to
                # seq[i-k]
                if maxtap < 0:
                    offset = abs(maxtap)
                else:
                    offset = 0
                if maxtap == mintap and maxtap != 0:
                    nw_seq =seq['input'][:abs(maxtap)]
                elif maxtap -k != 0 :
                    nw_seq = seq['input'][offset +k -mintap: -(maxtap -k)]
                else:
                    nw_seq = seq['input'][offset +k -mintap: ]
                if go_backwards:
                    nw_seq = nw_seq[::-1]



                scan_seqs.append( nw_seq )
                inner_seqs.append( nw_slice )
                inner_slices.append( actual_slice )
                n_seqs += 1


    # Since we've added all sequences now we need to level them up based on
    # n_steps or their different shapes
    lengths_vec = []
    for seq in scan_seqs:
        lengths_vec.append( seq.shape[0] )

    if not scan_utils.isNaN_or_Inf_or_None(n_steps):
        # ^ N_steps should also be considered
        lengths_vec.append( tensor.as_tensor(n_steps) )


    if len(lengths_vec) == 0 :
        # ^ No information about the number of steps
        raise ValueError(' No information about the number of steps '
                         'provided. Either provide a value for '
                         'n_steps argument of scan or provide an input '
                         'sequence')

    # If the user has provided the number of steps, do that regardless ( and
    # raise an error if the sequences are not long enough )
    if scan_utils.isNaN_or_Inf_or_None(n_steps):
        actual_n_steps = lengths_vec[0]
        for contestant in lengths_vec[1:]:
            actual_n_steps = tensor.minimum(actual_n_steps, contestant)
    else:
        actual_n_steps = tensor.as_tensor(n_steps)

    # Add names -- it helps a lot when debugging
    for (nw_seq, seq) in zip(scan_seqs, seqs):
        if getattr(seq['input'],'name', None) is not None:
            nw_seq.name = seq['input'].name + '[%d:]'%k

    # Conventions :
    #   mit_mot = multiple input taps, multiple output taps ( only provided
    #             by the gradient function )
    #   mit_sot = multiple input taps, single output tap (t + 0)
    #   sit_sot = single input tap, single output tap (t + 0)
    #   nit_sot = no input tap, single output tap (t + 0)


    # MIT_MOT -- not provided by the user only by the grad function
    n_mit_mot             = 0
    n_mit_mot_outs        = 0
    mit_mot_scan_inputs   = []
    mit_mot_inner_inputs  = []
    mit_mot_inner_outputs = []
    mit_mot_out_slices    = []
    mit_mot_rightOrder    = []



    # SIT_SOT -- provided by the user
    n_mit_sot             = 0
    mit_sot_scan_inputs   = []
    mit_sot_inner_inputs  = []
    mit_sot_inner_slices  = []
    mit_sot_inner_outputs = []
    mit_sot_return_steps  = {}
    mit_sot_tap_array     = []
    mit_sot_rightOrder    = []

    n_sit_sot             = 0
    sit_sot_scan_inputs   = []
    sit_sot_inner_inputs  = []
    sit_sot_inner_slices  = []
    sit_sot_inner_outputs = []
    sit_sot_return_steps  = {}
    sit_sot_rightOrder    = []


    # go through outputs picking up time slices as needed
    for i,init_out in enumerate(outs_info):
        # Note that our convention dictates that if an output uses
        # just the previous time step, as a initial state we will only
        # provide a tensor of the same dimension as one time step; This
        # makes code much cleaner for those who do not use taps. Otherwise
        # they would always had to shape_padleft the initial state ..
        # which is ugly
        if init_out.get('taps', None) == [-1]:

            actual_arg = init_out['initial']
            arg = safe_new(init_out['initial'])

            # Try to transfer test_value to the new variable
            if config.compute_test_value != 'off':
                try:
                    arg.tag.test_value = gof.Op._get_test_value(actual_arg)
                except AttributeError, e:
                    if config.compute_test_value != 'ignore':
                        # No need to print a warning or raise an error now,
                        # it will be done when fn will be called.
                        info(('Cannot compute test value for the inner '
                            'function of scan, input value missing'), e)

            if getattr(init_out['initial'],'name', None) is not None:
                arg.name = init_out['initial'].name+'[t-1]'

            # We need now to allocate space for storing the output and copy
            # the initial state over. We do this using the expand function
            # defined in scan utils
            sit_sot_scan_inputs.append(
                scan_utils.expand(
                    tensor.unbroadcast(
                        tensor.shape_padleft(actual_arg), 0)
                    , actual_n_steps
                ) )

            sit_sot_inner_slices.append(actual_arg)
            if i in return_steps:
                sit_sot_return_steps[n_sit_sot] = return_steps[i]
            sit_sot_inner_inputs.append( arg )
            sit_sot_rightOrder.append( i )
            n_sit_sot += 1

        elif init_out.get('taps',None):

            if numpy.any(numpy.array(init_out.get('taps',[])) > 0):
                # Make sure we do not have requests for future values of a
                # sequence we can not provide such values
                raise ValueError('Can not use future taps of outputs'
                                    , init_out)
            # go through the taps
            mintap = abs(numpy.min(init_out['taps']))
            mit_sot_tap_array.append( init_out['taps'] )
            idx_offset = abs(numpy.min(init_out['taps']))
            # Sequence
            mit_sot_scan_inputs.append(
                scan_utils.expand( init_out['initial'][:mintap]
                                 , actual_n_steps) )

            if i in return_steps:
                mit_sot_return_steps[n_mit_sot] = return_steps[i]
            mit_sot_rightOrder.append( i )
            n_mit_sot += 1
            for k in init_out['taps']:
                # create a new slice
                actual_nw_slice = init_out['initial'][k+mintap]
                _init_out_var = tensor.as_tensor_variable(init_out['initial'])
                _init_out_var_slice = _init_out_var[k+mintap]
                nw_slice = _init_out_var_slice.type()

                # Try to transfer test_value to the new variable
                if config.compute_test_value != 'off':
                    try:
                        nw_slice.tag.test_value = Op._get_test_value(_init_out_var_slice)
                    except AttributeError, e:
                        if config.compute_test_value != 'ignore':
                            # No need to print a warning or raise an error now,
                            # it will be done when fn will be called.
                            info(('Cannot compute test value for the inner '
                                'function of scan, input value missing.'), e)

                # give it a name or debugging and pretty printing
                if getattr(init_out['initial'],'name', None) is not None:
                    if k > 0:
                        nw_slice.name = ( init_out['initial'].name +
                                            '[t+%d]'%k )
                    elif k == 0:
                        nw_slice.name = init_out['initial'].name + '[t]'
                    else:
                        nw_slice.name = ( init_out['initial'].name +
                                            '[t%d]'%k )
                mit_sot_inner_inputs.append( nw_slice )
                mit_sot_inner_slices.append( actual_nw_slice )
        #NOTE: there is another case, in which we do not want to provide
        #      any previous value of the output to the inner function (i.e.
        #      a map); in that case we do not have to do anything ..

    # Re-order args
    max_mit_sot = numpy.max( [-1] + mit_sot_rightOrder ) + 1
    max_sit_sot = numpy.max( [-1] + sit_sot_rightOrder ) + 1
    n_elems     = numpy.max( [ max_mit_sot, max_sit_sot ] )
    _ordered_args = [[] for x in xrange(n_elems)]
    offset = 0
    for idx in xrange(n_mit_sot):
        n_inputs = len(mit_sot_tap_array[idx])
        if n_fixed_steps in [1,-1]:
            _ordered_args[mit_sot_rightOrder[idx]] = \
                            mit_sot_inner_slices[offset:offset+n_inputs]
        else:
            _ordered_args[mit_sot_rightOrder[idx]] = \
                            mit_sot_inner_inputs[offset:offset+n_inputs]
        offset += n_inputs

    for idx in xrange(n_sit_sot):
        if n_fixed_steps in [1,-1]:
            _ordered_args[sit_sot_rightOrder[idx]] = \
                                        [ sit_sot_inner_slices[idx] ]
        else:
            _ordered_args[sit_sot_rightOrder[idx]] = \
                                        [ sit_sot_inner_inputs[idx] ]

    ordered_args = []
    for ls in _ordered_args:
        ordered_args += ls
    if n_fixed_steps in [1,-1]:
        args = (inner_slices +
                ordered_args +
                non_seqs     )

    else:
        args = ( inner_seqs  +
                ordered_args +
                non_seqs     )

    # add only the non-shared variables to the arguments of the dummy
    # function [ a function should not get shared variables as input ]
    # this could happen if for example the initial state of an output is a
    # shared variable for which we use only the last step (i.e. no
    # subtensort is applied to the shared variable )
    dummy_args = [arg for arg in args
                  if not isinstance(arg, SharedVariable)]
    # when we apply the lambda expression we get a mixture of update rules
    # and outputs that needs to be separated

    outputs, updates = scan_utils.get_updates_and_outputs(fn(*args))
    ##
    ###   Step 3. Check if we actually need scan and remove it if we don't
    ##


    if n_fixed_steps in [1, -1]:
        # We do not need to use the scan op anymore, so we can just return
        # the outputs and updates we have

        for pos, inner_out in enumerate(outputs):
            # we need to see if we need to pad our sequences with an
            # unbroadcastable dimension; case example : we return an
            # output for which we want all intermediate. If n_steps is 1
            # then, if we return the output as given by the innner function
            # this will represent only a slice and it will have one
            # dimension less.
            if ( isinstance(inner_out.type, tensor.TensorType) and
                return_steps.get(pos, 0) != 1):
                outputs[pos] = tensor.unbroadcast(
                    tensor.shape_padleft(inner_out),0)
        if len(outputs) == 1:
            outputs = outputs[0]

        return (outputs, updates)


    ##
    ###   Step 4. Compile the dummy function
    ##

    # We can now compile a dummy function just to see what shared variable
    # we have and what are their update rules (note that the user has
    # the option not to pass the shared variable to scan, so we need to
    # pick them manually and add them to scan)
    # make the compilation as fast as possible by not applying any
    # optimization or conversion to C [ note this region is not important
    # for performance so we can do stuff as unoptimal as we wish ]

    # extract still missing inputs (there still might be so) and add them
    # as non sequences at the end of our args

    all_inputs = itertools.ifilter(
        lambda x: ( isinstance(x, gof.Variable) and
                   not isinstance(x, SharedVariable) and
                   not isinstance(x, gof.Constant) ),
        gof.graph.inputs( outputs) )
    extra_inputs     = filter( lambda x: x not in args,
                                    all_inputs)
    non_seqs += extra_inputs
    ## Note we do not use all_inputs directly since the order of variables
    ## in args is quite important
    dummy_args += extra_inputs

    dummy_f = function( dummy_args
                       , outputs
                       , updates = updates
                       , mode = compile.mode.Mode(linker='py',
                                                  optimizer=None) )


    ##
    ### Step 5. Re-arange inputs of scan into a more strict order
    ##

    ## Step 5.0 Check the outputs of the dummy function to see if they
    ##          match with user provided data


    # if the number of outputs to the function does not match the number of
    # assumed outputs until now (provided by the user) there can be
    # only one explanation: No information is provided for any of the
    # outputs (i.e. we are dealing with a map)
    if not ( len(dummy_f.maker.outputs) == n_outs or outs_info == []):
        raise ValueError('Please provide None as output_info for '
                         'any output that does not feed back into '
                         'scan (i.e. it behaves like a map) ')

    if outs_info == []:
        n_outs = len(dummy_f.maker.outputs)
        outs_info   = [ dict() for x in xrange(n_outs) ]


    ## Step 5.1 Outputs with taps different then -1

    for i, out in enumerate(outs_info):
        if 'taps' in out and out['taps'] != [-1]:
            mit_sot_inner_outputs.append( outputs[i])


    ## Step 5.2 Outputs with tap equal to -1
    for i, out in enumerate(outs_info):
        if 'taps' in out and out['taps'] == [-1]:
            sit_sot_inner_outputs.append( outputs[i] )


    ## Step 5.3 Outputs that correspond to update rules of shared variables
    givens               = {}
    n_shared_outs        = 0
    shared_scan_inputs   = []
    shared_inner_inputs  = []
    shared_inner_outputs = []
    for input in dummy_f.maker.expanded_inputs:
        if isinstance(input.variable, SharedVariable) and input.update:
            new_var = safe_new(input.variable)
            if getattr(input.variable,'name', None) is not None:
                new_var.name = input.variable.name + '_copy'
            shared_inner_inputs.append( new_var )
            shared_scan_inputs.append( input.variable )
            shared_inner_outputs.append( input.update )
            givens[input.variable] = new_var
            n_shared_outs += 1

    ## Step 5.4 Outputs with no taps used in the input
    n_nit_sot      = 0
    nit_sot_inner_outputs = []
    nit_sot_return_steps  = {}
    nit_sot_rightOrder    = []
    for i,out in enumerate(outs_info):
        if not 'taps' in out:
            nit_sot_inner_outputs.append( outputs[i] )
            if i in return_steps:
                nit_sot_return_steps[n_nit_sot] = return_steps[i]
            nit_sot_rightOrder.append( i )
            n_nit_sot += 1

    ## Step 5.5 all other arguments including extra inputs
    other_scan_args  = []
    other_inner_args = []

    other_scan_args  += [ arg for arg in non_seqs
                        if not isinstance(arg, SharedVariable) ]

    ## Step 5.6 all non sequences including shared variables with no update rules
    def new_variable( v ):
        if isinstance(new_variable, tensor.Constant):
            return v.clone()
        new_v = safe_new(v)
        if getattr(v,'name', None) is not None:
            new_v.name = v.name + '_copy'
        return new_v
    other_inner_args += [ new_variable(arg) for arg in non_seqs
                         if not isinstance(arg, SharedVariable) ]
    givens.update( dict( zip(other_scan_args, other_inner_args) ))
    other_shared_scan_args  = [ arg.variable for arg
                        in dummy_f.maker.expanded_inputs
                        if ( isinstance(arg.variable, SharedVariable) and
                            not arg.update) ]
    other_shared_inner_args = [ new_variable(arg.variable) for arg
                        in dummy_f.maker.expanded_inputs
                        if ( isinstance(arg.variable, SharedVariable) and
                            not arg.update) ]
    givens.update( dict( zip( other_shared_scan_args,
                             other_shared_inner_args) ) )


    ##
    ### Step 6. Re-order the outputs and clone them replacing things
    ###         using the givens
    ##
    inner_inputs = ( inner_seqs             +
                    mit_mot_inner_inputs    +
                    mit_sot_inner_inputs    +
                    sit_sot_inner_inputs    +
                    shared_inner_inputs     +
                    other_shared_inner_args +
                    other_inner_args        )

    inner_outs = ( mit_mot_inner_outputs +
                   mit_sot_inner_outputs +
                   sit_sot_inner_outputs +
                   nit_sot_inner_outputs +
                   shared_inner_outputs  )
    if cuda.cuda_available:
        # very often we end up in this situation when we want to
        # replace w with w_copy, where w is CudaNdarray
        # and w_copy is TensorType. This is caused because shared
        # variables are put on GPU right aways >:| ,
        new_givens = {}


        for w,w_copy in givens.iteritems():
            if (isinstance(w.type, cuda.CudaNdarrayType)
                and isinstance(w_copy.type, tensor.TensorType)):
                for o in inner_outs:
                    new_givens = traverse(o,w,w_copy, new_givens)
            else:
                new_givens[w] = w_copy
    else:
        new_givens = givens

    new_outs = scan_utils.clone(inner_outs, replace = new_givens)

    ##
    ### Step 7. Create the Scan Op
    ##

    tap_array = mit_sot_tap_array + [[-1] for x in xrange(n_sit_sot)]
    info      = {}

    info['tap_array']          = tap_array
    info['n_seqs']             = n_seqs
    info['n_mit_mot']          = n_mit_mot
    info['n_mit_mot_outs']     = n_mit_mot_outs
    info['mit_mot_out_slices'] = mit_mot_out_slices
    info['n_mit_sot']          = n_mit_sot
    info['n_sit_sot']          = n_sit_sot
    info['n_shared_outs']      = n_shared_outs
    info['n_nit_sot']          = n_nit_sot
    info['truncate_gradient']  = truncate_gradient
    info['name']               = name
    info['mode']               = mode
    info['inplace']            = False
    info['gpu']                = False

    local_op = scan_op.Scan( inner_inputs, new_outs, info )

    ##
    ### Step 8. Compute the outputs using the scan op
    ##
    scan_inputs = ( scan_seqs                                    +
                   mit_mot_scan_inputs                           +
                   mit_sot_scan_inputs                           +
                   sit_sot_scan_inputs                           +
                   shared_scan_inputs                            +
                   [ actual_n_steps for x in xrange(n_nit_sot) ] +
                   other_shared_scan_args                        +
                   other_scan_args                               )

    scan_inputs = [safe_to_cpu(x) for x in ([actual_n_steps] + scan_inputs)]
    scan_outs = local_op(* scan_inputs  )
    if type(scan_outs) not in (list,tuple):
        scan_outs = [scan_outs]
    ##
    ### Step 9. Figure out which outs are update rules for shared variables
    ###         and so on ...
    ##

    update_map = Updates()
    def remove_dimensions( outs, steps_return, offsets = None):
        out_ls = []
        for idx, out in enumerate(outs):
            if idx in steps_return:
                if steps_return[idx] > 1:
                    out_ls.append( out[-steps_return[idx]:] )
                else:
                    out_ls.append( out[-1] )
            else:
                if offsets is None:
                    out_ls.append( out )
                else:
                    out_ls.append( out[offsets[idx]:] )
        return out_ls

    offset = n_mit_mot
    offsets = [ abs(numpy.min(x)) for x in mit_sot_tap_array ]
    mit_sot_outs = remove_dimensions(
        scan_outs[offset:offset+n_mit_sot]
        , mit_sot_return_steps
        , offsets                   )

    offset += n_mit_sot
    offsets = [ 1 for x in xrange(n_sit_sot) ]
    sit_sot_outs = remove_dimensions(
        scan_outs[offset:offset+n_sit_sot]
        , sit_sot_return_steps
        , offsets                   )

    offset += n_sit_sot
    nit_sot_outs = remove_dimensions(
        scan_outs[offset:offset+n_nit_sot]
        , nit_sot_return_steps )

    offset += n_nit_sot
    for idx, update_rule in enumerate(scan_outs[offset:offset+n_shared_outs]):
        update_map[shared_scan_inputs[idx]] = update_rule

    _scan_out_list = ( mit_sot_outs +
                      sit_sot_outs  +
                      nit_sot_outs  )
    # Step 10. I need to reorder the outputs to be in the order expected by
    # the user
    rightOrder = ( mit_sot_rightOrder +
                  sit_sot_rightOrder  +
                  nit_sot_rightOrder  )
    scan_out_list = [None]*len(rightOrder)
    for idx,pos in enumerate(rightOrder):
        scan_out_list[pos] =  _scan_out_list[idx]
    if len(scan_out_list) == 1:
        scan_out_list = scan_out_list[0]
    elif len(scan_out_list) == 0:
        scan_out_list = None

    return (scan_out_list, update_map)