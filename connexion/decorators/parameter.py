import copy
import functools
import inspect
import logging
import re

import inflection
import six

from ..lifecycle import ConnexionRequest  # NOQA
from ..utils import all_json, boolean, is_null, is_nullable

try:
    import builtins
except ImportError:  # pragma: no cover
    import __builtin__ as builtins


logger = logging.getLogger(__name__)

# Python 2/3 compatibility:
try:
    py_string = unicode
except NameError:  # pragma: no cover
    py_string = str  # pragma: no cover

# https://github.com/swagger-api/swagger-spec/blob/master/versions/2.0.md#data-types
TYPE_MAP = {'integer': int,
            'number': float,
            'string': py_string,
            'boolean': boolean,
            'array': list,
            'object': dict}  # map of swagger types to python types


def inspect_function_arguments(function):  # pragma: no cover
    """
    Returns the list of variables names of a function and if it
    accepts keyword arguments.

    :type function: Callable
    :rtype: tuple[list[str], bool]
    """
    if six.PY3:
        parameters = inspect.signature(function).parameters
        bound_arguments = [name for name, p in parameters.items()
                           if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
        has_kwargs = any(p.kind == p.VAR_KEYWORD for p in parameters.values())
        return list(bound_arguments), has_kwargs
    else:
        argspec = inspect.getargspec(function)
        return argspec.args, bool(argspec.keywords)


def make_type(value, type):
    type_func = TYPE_MAP[type]  # convert value to right type
    return type_func(value)


def get_val_from_body(value, body):
    if is_nullable(body) and is_null(value):
        return None

    if body is not None:
        body = body.get("schema", body)

    if "type" not in body:
        logger.error(body)
        raise Exception("wtf! {body} {value}".format(body=body, value=value))

    if body["type"] == "array":
        return [make_type(v, body["items"].get("schema", body["items"])["type"])
                for v in value]
    else:
        return make_type(value, body["type"])


def get_val_from_param(value, query_param):
    if is_nullable(query_param) and is_null(value):
        return None

    query_defn = query_param.get("schema", query_param)  # oas3

    if query_defn["type"] == "array":  # then logic is more complex
        # TODO DGK refactor common code with validation
        try:
            # oas3
            style = query_param["style"]
            # default values for "in"
            # query - form; path - simple; header - simple; cookie - form.
            delimiters = {
                "spaceDelimited": " ",
                "pipeDelimited": "|",
                "simple": ","
            }
            parts = value.split(delimiters.get(style, ","))
        except KeyError:
            # swagger2
            if query_param.get("collectionFormat") == "pipes":
                parts = value.split("|")
            else:  # default: csv
                parts = value.split(",")
        return [make_type(part, query_defn["items"]["type"]) for part in parts]
    else:
        return make_type(value, query_defn["type"])


def snake_and_shadow(name):
    """
    Converts the given name into Pythonic form. Firstly it converts CamelCase names to snake_case. Secondly it looks to
    see if the name matches a known built-in and if it does it appends an underscore to the name.
    :param name: The parameter name
    :type name: str
    :return:
    """
    snake = inflection.underscore(name)
    if snake in builtins.__dict__.keys():
        return "{}_".format(snake)
    return snake


def parameter_to_arg(parameters, body_schema, consumes, function, pythonic_params=False):
    """
    Pass query and body parameters as keyword arguments to handler function.

    See (https://github.com/zalando/connexion/issues/59)
    :param parameters: All the schema parameters of the handler functions
    :type parameters: dict|None
    :param body_schema: All the schema parameters of the handler functions
    :type body_schema: dict|None
    :param consumes: The list of content types the operation consumes
    :type consumes: list
    :param function: The handler function for the REST endpoint.
    :param pythonic_params: When True CamelCase parameters are converted to snake_case and an underscore is appended to
    any shadowed built-ins
    :type pythonic_params: bool
    :type function: function|None
    """
    def sanitize_param(name):
        if name and pythonic_params:
            name = snake_and_shadow(name)
        return name and re.sub('^[^a-zA-Z_]+', '', re.sub('[^0-9a-zA-Z_]', '', name))

    def make_request_query(request):
        request_query = {}
        try:
            for k, v in request.query.to_dict(flat=False).items():
                k = sanitize_param(k)
                query_param = query_types.get(k, None)
                # OAS3 schema keyword
                query_schema = query_param.get('schema', query_param)
                if (query_schema is not None and query_schema['type'] == 'array'):
                    try:
                        # OAS3
                        style = query_param['style']
                        explode = query_param.get('explode', style == 'form')
                        delimiters = {
                            'spaceDelimited': ' ',
                            'pipeDelimited': '|',
                            'simple': ',',
                            'form': ','
                        }
                        if explode:
                            request_query[k] = delimiters.get(style, ',').join(v)
                        else:
                            request_query[k] = v[-1]
                    except KeyError:
                        if query_param.get('collectionFormat') == 'multi':
                            request_query[k] = ','.join(v)
                        else:
                            request_query[k] = v[-1]
                else:
                    request_query[k] = v[-1]
        except AttributeError:
            request_query = {sanitize_param(k): v for k, v in request.query.items()}
        return request_query

    # swagger2 body
    body_parameters = [parameter for parameter in parameters if parameter['in'] == 'body'] or [{}]
    body_name = sanitize_param(body_parameters[0].get('name'))
    default_body = body_parameters[0].get('schema', {}).get('default')

    form_types = {sanitize_param(parameter['name']): parameter
                  for parameter in parameters
                  if parameter['in'] == 'formData'}

    # openapi3 body
    if body_name is None and body_schema is not None:
        logger.debug('body schema is %s', body_schema)
        body_properties = {sanitize_param(key): value
                           for key, value
                           in body_schema.get('properties', {}).items()}
        default_body = body_schema.get('default', default_body)
    else:
        body_properties = {}

    query_types = {sanitize_param(parameter['name']): parameter
                   for parameter in parameters if parameter['in'] == 'query'}  # type: dict[str, str]
    path_types = {parameter['name']: parameter
                  for parameter in parameters
                  if parameter['in'] == 'path'}
    arguments, has_kwargs = inspect_function_arguments(function)
    default_query_params = {sanitize_param(param['name']): param.get('schema', param)['default']
                            for param in parameters
                            if param['in'] == 'query' and 'default' in param.get('schema', param)}
    default_form_params = {sanitize_param(param['name']): param.get('schema', param)['default']
                           for param in parameters
                           if param['in'] == 'formData' and 'default' in param.get('schema', param)}

    @functools.wraps(function)
    def wrapper(request):
        # type: (ConnexionRequest) -> Any
        logger.debug('Function Arguments: %s', arguments)
        kwargs = {}

        if all_json(consumes):
            request_body = request.json
        elif 'application/x-www-form-urlencoded' == consumes[0]:
            request_body = {sanitize_param(k): v for k, v in dict(request.form.items()).items()}
        elif 'multipart/form-data' == consumes[0]:
            request_body = {sanitize_param(k): v for k, v in dict(request.form.items()).items()}
        else:
            request_body = request.body

        if default_body and not request_body:
            request_body = default_body

        # Parse path parameters
        path_params = request.path_params
        for key, value in path_params.items():
            key = sanitize_param(key)
            if key in path_types:
                kwargs[key] = get_val_from_param(value, path_types[key])
            else:  # Assume path params mechanism used for injection
                kwargs[key] = value

        if body_schema and body_name is None:
            x_body_name = body_schema.get('x-body-name', 'body')
            logger.debug('x-body-name is %s' % x_body_name)
            if x_body_name in arguments or has_kwargs:
                val = get_val_from_body(request_body, body_schema)
                kwargs[x_body_name] = val

        # swagger2 body param and formData
        # Add body parameters
        if body_name:
            if not has_kwargs and body_name not in arguments:
                logger.debug("Body parameter '%s' not in function arguments", body_name)
            else:
                logger.debug("Body parameter '%s' in function arguments", body_name)
                kwargs[body_name] = request_body

        if not body_properties:
            # swagger 2
            # Add formData parameters
            form_arguments = copy.deepcopy(default_form_params)
            form_arguments.update({sanitize_param(k): v for k, v in request.form.items()})
            for key, value in form_arguments.items():
                if not has_kwargs and key not in arguments:
                    logger.debug("FormData parameter '%s' not in function arguments", key)
                else:
                    logger.debug("FormData parameter '%s' in function arguments", key)
                    try:
                        form_param = form_types[key]
                    except KeyError:  # pragma: no cover
                        logger.error("Function argument '{}' not defined in specification".format(key))
                    else:
                        kwargs[key] = get_val_from_param(value, form_param)

        # Add query parameters
        query_arguments = copy.deepcopy(default_query_params)
        query_arguments.update(make_request_query(request))
        for key, value in query_arguments.items():
            key = sanitize_param(key)
            if not has_kwargs and key not in arguments:
                logger.debug("Query Parameter '%s' not in function arguments", key)
            else:
                logger.debug("Query Parameter '%s' in function arguments", key)
                try:
                    query_param = query_types[key]
                except KeyError:  # pragma: no cover
                    logger.error("Function argument '{}' not defined in specification".format(key))
                else:
                    logger.debug('%s is a %s', key, query_param)
                    kwargs[key] = get_val_from_param(value, query_param)

        # Add file parameters
        file_arguments = request.files
        for key, value in file_arguments.items():
            if not has_kwargs and key not in arguments:
                logger.debug("File parameter (formData) '%s' not in function arguments", key)
            else:
                logger.debug("File parameter (formData) '%s' in function arguments", key)
                kwargs[key] = value

        # optionally convert parameter variable names to un-shadowed, snake_case form
        if pythonic_params:
            kwargs = {snake_and_shadow(k): v for k, v in kwargs.items()}

        # add context info (e.g. from security decorator)
        for key, value in request.context.items():
            if has_kwargs or key in arguments:
                kwargs[key] = value
            else:
                logger.debug("Context parameter '%s' not in function arguments", key)
        return function(**kwargs)

    return wrapper
