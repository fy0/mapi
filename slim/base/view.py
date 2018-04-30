import asyncio
import logging
import time
from abc import abstractmethod
from types import FunctionType
from typing import Tuple, Union, Dict, Iterable, Type, List, Set
from aiohttp import web

from .sqlquery import SQLQueryInfo, SQL_TYPE, SQLForeignKey, SQLValuesToWrite, ALL_COLUMNS, PRIMARY_KEY, SQL_OP
from .app import Application
from .helper import create_signed_value, decode_signed_value
from .permission import Permissions, Ability, BaseUser, A, DataRecord
from .sqlfuncs import AbstractSQLFunctions
from ..retcode import RETCODE
from ..utils import pagination_calc, MetaClassForInit, async_call, is_py36
from ..utils.json_ex import json_ex_dumps
from ..exception import RecordNotFound, SyntaxException, InvalidParams, SQLOperatorInvalid, ColumnIsNotForeignKey, \
    ColumnNotFound, RoleNotFound, PermissionDenied, FinishQuitException, SlimException, TableNotFound, \
    ResourceException, NotNullConstraintFailed, AlreadyExists

logger = logging.getLogger(__name__)


class BaseView(metaclass=MetaClassForInit):
    """
    应在 cls_init 时完成全部接口的扫描与wrap函数创建
    并在wrapper函数中进行实例化，传入 request 对象
    """
    _interface = {}
    _no_route = False
    # permission: Permissions  # 3.6

    @classmethod
    def use(cls, name, method: [str, Set, List], url=None):
        """ interface helper function"""
        if not isinstance(method, (str, list, set, tuple)):
            raise BaseException('Invalid type of method: %s' % type(method).__name__)

        if isinstance(method, str):
            method = {method}

        # TODO: check methods available
        cls._interface[name] = [{'method': method, 'url': url}]

    @classmethod
    def use_lst(cls, name):
        cls._interface[name] = [
            {'method': {'GET'}, 'url': '%s/{page}' % name},
            {'method': {'GET'}, 'url': '%s/{page}/{size}' % name},
        ]

    @classmethod
    def discard(cls, name):
        """ interface helper function"""
        cls._interface.pop(name, None)

    @classmethod
    def interface(cls):
        pass

    @classmethod
    def permission_init(cls):
        """ Override it """
        cls.permission.add(Ability(None, {'*': '*'}))

    @classmethod
    def cls_init(cls):
        cls._interface = {}
        cls.interface()
        for k, v in vars(cls).items():
            if isinstance(v, FunctionType):
                if getattr(v, '_interface', None):
                    cls.use(k, *v._interface)
        if getattr(cls, 'permission', None):
            cls.permission = cls.permission.copy()
        else:
            cls.permission = Permissions()
        cls.permission_init()

    def __init__(self, app: Application, aiohttp_request: web.web_request.Request):
        self.app = app
        self._request = aiohttp_request

        self.ret_val = None
        self.response = None
        self.session = None
        self._cookie_set = None
        self._params_cache = None
        self._post_data_cache = None
        self._post_json_cache = None
        self._current_user = None

    @property
    def is_finished(self):
        return self.response is not None

    async def _prepare(self):
        session_cls = self.app.options.session_cls
        self.session = await session_cls.get_session(self)

    async def prepare(self):
        pass

    async def _on_finish(self):
        if self.session:
            await self.session.save()

    async def on_finish(self):
        pass

    @property
    def current_user(self) -> BaseUser:
        if not self._current_user:
            if getattr(self, 'get_current_user', None):
                self._current_user = self.get_current_user()
            else:
                self._current_user = None
        return self._current_user

    @property
    def current_user_roles(self):
        u = self.current_user
        if u is None:
            return {None}
        return u.roles

    def finish(self, code, data=NotImplemented):
        if data is NotImplemented:
            data = RETCODE.txt_cn.get(code)
        self.ret_val = {'code': code, 'data': data}  # for access in inhreads method
        self.response = web.json_response(self.ret_val, dumps=json_ex_dumps)
        logger.debug('finish: %s' % self.ret_val)
        for i in self._cookie_set or ():
            if i[0] == 'set':
                self.response.set_cookie(i[1], i[2], **i[3])
            else:
                self.response.del_cookie(i[1])

    def del_cookie(self, key):
        if self._cookie_set is None:
            self._cookie_set = []
        self._cookie_set.append(('del', key))

    @property
    def params(self) -> dict:
        if self._params_cache is None:
            self._params_cache = dict(self._request.query)
        return self._params_cache

    async def _post_json(self) -> dict:
        # post body: raw(text) json
        if self._post_json_cache is None:
            self._post_json_cache = dict(await self._request.json())
        return self._post_json_cache

    async def post_data(self) -> dict:
        # post body: form data
        if self._post_data_cache is None:
            self._post_data_cache = dict(await self._request.post())
            logger.debug('raw post data: %s', self._post_data_cache)
        return self._post_data_cache

    def set_cookie(self, key, value, *, path='/', expires=None, domain=None, max_age=None, secure=None,
                   httponly=None, version=None):
        if self._cookie_set is None:
            self._cookie_set = []
        kwargs = {'path': path, 'expires': expires, 'domain': domain, 'max_age': max_age, 'secure': secure,
                  'httponly': httponly, 'version': version}
        self._cookie_set.append(('set', key, value, kwargs))

    def get_cookie(self, name, default=None):
        if self._request.cookies is not None and name in self._request.cookies:
            return self._request.cookies.get(name)
        return default

    def set_secure_cookie(self, name, value: bytes, *, httponly=True, max_age=30):
        #  一般来说是 UTC
        # https://stackoverflow.com/questions/16554887/does-pythons-time-time-return-a-timestamp-in-utc
        timestamp = int(time.time())
        # version, utctime, name, value
        # assert isinatance(value, (str, list, tuple, bytes, int))
        to_sign = [1, timestamp, name, value]
        secret = self.app.options.cookies_secret
        self.set_cookie(name, create_signed_value(secret, to_sign), max_age=max_age, httponly=httponly)

    def get_secure_cookie(self, name, default=None, max_age_days=31):
        secret = self.app.options.cookies_secret
        value = self.get_cookie(name)
        if value:
            data = decode_signed_value(secret, value)
            # TODO: max_age_days 过期计算
            if data and data[2] == name:
                return data[3]
        return default

    @property
    def headers(self):
        return self._request.headers

    @property
    def route_info(self):
        """
        info matched by router
        :return:
        """
        return self._request.match_info

    @classmethod
    def _ready(cls):
        """ private version of cls.ready() """
        cls.ready()

    @classmethod
    def ready(cls):
        """
        All modules loaded, and ready to serve.
        Emitted after register routes and before loop start
        :return:
        """
        pass


class ViewOptions:
    def __init__(self, *, list_page_size=20, list_accept_size_from_client=False, permission: Permissions=None):
        self.list_page_size = list_page_size
        self.list_accept_size_from_client = list_accept_size_from_client
        self.permission = permission

    def assign(self, obj: Type['AbstractSQLView']):
        obj.LIST_PAGE_SIZE = self.list_page_size
        obj.LIST_ACCEPT_SIZE_FROM_CLIENT = self.list_accept_size_from_client
        if self.permission:
            obj.permission = self.permission


class ErrorCatchContext:
    def __init__(self, view: BaseView):
        self.view = view

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val: Exception, exc_tb):
        # FinishQuitException
        if isinstance(exc_val, FinishQuitException):
            return  # Do nothing

        # SyntaxException
        elif isinstance(exc_val, SyntaxException):
            self.view.finish(RETCODE.FAILED, exc_val.args[0])

        # ParamsException
        elif isinstance(exc_val, SQLOperatorInvalid):
            self.view.finish(RETCODE.INVALID_PARAMS, "Invalid operator for select condition: %r" % exc_val.args[0])

        elif isinstance(exc_val, ColumnIsNotForeignKey):
            self.view.finish(RETCODE.INVALID_PARAMS, "This column is not a foreign key: %r" % exc_val.args[0])

        # ResourceException
        elif isinstance(exc_val, TableNotFound):
            self.view.finish(RETCODE.FAILED, exc_val.args[0])

        elif isinstance(exc_val, ColumnNotFound):
            self.view.finish(RETCODE.FAILED, "Column not found: %r" % exc_val.args[0])

        elif isinstance(exc_val, RecordNotFound):
            self.view.finish(RETCODE.NOT_FOUND)

        elif isinstance(exc_val, NotNullConstraintFailed):
            self.view.finish(RETCODE.INVALID_POSTDATA, 'NOT NULL constraint failed')

        elif isinstance(exc_val, AlreadyExists):
            self.view.finish(RETCODE.ALREADY_EXISTS)

        elif isinstance(exc_val, ResourceException):
            self.view.finish(RETCODE.FAILED, exc_val.args[0])

        # PermissionException
        elif isinstance(exc_val, RoleNotFound):
            self.view.finish(RETCODE.INVALID_ROLE, "Invalid role: %r" % exc_val.args[0])

        elif isinstance(exc_val, PermissionDenied):
            self.view.finish(RETCODE.PERMISSION_DENIED, exc_val.args[0])

        elif isinstance(exc_val, SlimException):
            if exc_val.args[0] == 'bad value':
                self.view.finish(RETCODE.INVALID_PARAMS, exc_val.args[0])
            else:
                self.view.finish(RETCODE.FAILED)

        else: return  # 异常会传递出去
        return True


class AbstractSQLView(BaseView):
    _sql_cls = AbstractSQLFunctions
    is_base_class = True  # skip cls_init check

    options_cls = ViewOptions
    LIST_PAGE_SIZE = 20  # list 单次取出的默认大小
    LIST_ACCEPT_SIZE_FROM_CLIENT = False

    table_name = None
    primary_key = None
    fields = {}
    foreign_keys = {}
    foreign_keys_table_alias = {}

    if is_py36:
        table_name: str = None
        primary_key: str = None
        fields: Dict[str, SQL_TYPE] = {}
        foreign_keys: Dict[str, List[SQLForeignKey]] = {}
        foreign_keys_table_alias: Dict[str, str] = {}  # hide real table name

    @classmethod
    def _is_skip_check(cls):
        skip_check = False
        if 'is_base_class' in cls.__dict__:
            skip_check = getattr(cls, 'is_base_class')
        return skip_check

    @classmethod
    def interface(cls):
        super().interface()
        cls.use('get', 'GET')
        cls.use_lst('list')
        cls.use('update', 'POST')
        cls.use('new', 'POST')
        cls.use('delete', 'POST')
        # deprecated
        cls.use('set', 'POST')

    @classmethod
    def add_soft_foreign_key(cls, column, table_name, alias=None):
        """
        the column stores foreign table's primary key but isn't a foreign key (to avoid constraint)
        warning: if the table not exists, will crash when query with loadfk
        :param column: table's column
        :param table_name: foreign table name
        :param alias: table name's alias, avoid exposed the table name to user. Default is as same as table name.
        :return: True, None
        """
        if column in cls.fields:
            table = SQLForeignKey(table_name, column, cls.fields[column], True)

            if alias:
                if alias in cls.foreign_keys_table_alias:
                    logger.warning("This alias of table is already exists, overwriting: %s.%s to %s" %
                                   (cls.__name__, column, table_name))
                cls.foreign_keys_table_alias[alias] = table

            if column not in cls.foreign_keys:
                cls.foreign_keys[column] = [table]
            else:
                if not alias:
                    logger.warning("The soft foreign key will not work, an alias required: %s.%s to %r" %
                                   (cls.__name__, column, table_name))
                cls.foreign_keys[column].append(table)
            return True

    @classmethod
    def _check_view_options(cls):
        options = getattr(cls, 'options', None)
        if options and isinstance(options, ViewOptions):
            options.assign(cls)

    @classmethod
    def cls_init(cls, check_options=True):
        if check_options:
            cls._check_view_options()

        # because of BaseView.cls_init is a bound method (@classmethod)
        # so we can only route BaseView._interface, not cls._interface defined by user
        BaseView.cls_init.__func__(cls)
        # super().cls_init()  # fixed in 3.6

        async def func():
            await cls._fetch_fields(cls)
            if not cls._is_skip_check():
                assert cls.table_name
                assert cls.fields
                # assert cls.primary_key
                # assert cls.foreign_keys

        asyncio.get_event_loop().run_until_complete(func())

    def _load_role(self, role):
        self.ability = self.permission.request_role(self.current_user, role)
        return self.ability

    @property
    def current_role(self) -> [int, str]:
        role_val = self.headers.get('Role')
        return int(role_val) if role_val and role_val.isdigit() else role_val

    async def _prepare(self):
        await super()._prepare()
        # _sql 里使用了 self.err 存放数据
        # 那么可以推测在并发中，cls._sql.err 会被多方共用导致出错
        self._sql = self._sql_cls(self.__class__)
        if not self._load_role(self.current_role):
            logger.debug("load role %r failed, please make sure the user is permitted"
                         " and the View object inherited a UserMixin." % self.current_role)
            self.finish(RETCODE.INVALID_ROLE)

    async def load_fk(self, info: SQLQueryInfo, records: Iterable[DataRecord]) -> Union[List, Iterable]:
        """
        :param info:
        :param records: the data got from database and filtered from permission
        :return:
        """
        # if not items, items is probably [], so return itself.
        # if not items: return items

        # 1. get tables' instances
        # table_map = {}
        # for column in info['loadfk'].keys():
        #     tbl_name = self.foreign_keys[column][0]
        #     table_map[column] = self.app.tables[tbl_name]

        # 2. get query parameters
        async def check(data, records):
            for column, fkvalues_lst in data.items():
                for fkvalues in fkvalues_lst:
                    pks = []
                    all_ni = True
                    for i in records:
                        val = i.get(column, NotImplemented)
                        if val != NotImplemented:
                            all_ni = False
                        pks.append(val)

                    if all_ni:
                        logger.debug("load foreign key failed, do you have read permission to the column %r?" % column)
                        continue

                    # 3. query foreign keys
                    vcls = self.app.tables[fkvalues['table']]
                    v = vcls(self.app, self._request)  # fake view
                    await v._prepare()
                    info2 = SQLQueryInfo()
                    info2.set_select(ALL_COLUMNS)
                    info2.add_condition(PRIMARY_KEY, 'in', pks)
                    info2.bind(v)

                    # ability = vcls.permission.request_role(self.current_user, fkvalues['role'])
                    # info2.check_query_permission_full(self.current_user, fktable, ability)

                    fk_record = await v._sql.select_one(info2)
                    if not fk_record: continue

                    fk_records = [fk_record]
                    await v.check_records_permission(info2, fk_records)

                    fk_dict = {}
                    for i in fk_records:
                        # 主键: 数据
                        fk_dict[i[vcls.primary_key]] = i

                    column_to_set = fkvalues.get('as', column) or column
                    for _, record in enumerate(records):
                        k = record.get(column, NotImplemented)
                        if k in fk_dict:
                            record[column_to_set] = fk_dict[k]

                    if fkvalues['loadfk']:
                        await check(fkvalues['loadfk'], fk_records)

        await check(info.loadfk, records)
        return records

    async def _call_handle(self, func, *args):
        """ call and check result of handle_query/read/insert/update """
        await async_call(func, *args)

        if self.is_finished:
            raise FinishQuitException()

    def _get_list_page_and_size(self) -> Tuple[int, int]:
        page = self.route_info.get('page', '1').strip()

        if not page.isdigit():
            raise InvalidParams("`page` is not a number")
        page = int(page)

        size = self.route_info.get('size', '').strip()
        if self.LIST_ACCEPT_SIZE_FROM_CLIENT:
            if size:
                if size == '-1':  # size is infinite
                    size = -1
                elif size.isdigit():  # isdigit means size >= 0
                    size = int(size or self.LIST_PAGE_SIZE)
                else:
                    raise InvalidParams("`size` is not a number")
            else:
                size = self.LIST_PAGE_SIZE
        else:
            size = self.LIST_PAGE_SIZE

        return page, size

    async def check_records_permission(self, info, records):
        for record in records:
            columns = record.set_info(info, self.ability, self.current_user)
            if not columns: raise RecordNotFound()
        await self._call_handle(self.after_read, records)

    async def get(self):
        with ErrorCatchContext(self):
            info = SQLQueryInfo(self.params, view=self)
            await self._call_handle(self.before_query, info)
            record = await self._sql.select_one(info)

            if record:
                records = [record]
                await self.check_records_permission(info, records)
                data_dict = await self.load_fk(info, records)
                self.finish(RETCODE.SUCCESS, data_dict[0])
            else:
                self.finish(RETCODE.NOT_FOUND)

    async def list(self):
        with ErrorCatchContext(self):
            page, size = self._get_list_page_and_size()
            info = SQLQueryInfo(self.params, view=self)
            await self._call_handle(self.before_query, info)
            records, count = await self._sql.select_page(info, size, page)
            await self.check_records_permission(info, records)

            if count:
                if size == -1: size = count
                pg = pagination_calc(count, size, page)
                records = await self.load_fk(info, records)
                pg["items"] = records

                self.finish(RETCODE.SUCCESS, pg)
            else:
                self.finish(RETCODE.NOT_FOUND)

    async def update(self):
        with ErrorCatchContext(self):
            info = SQLQueryInfo(self.params, self)
            raw_post = await self.post_data()
            values = SQLValuesToWrite(raw_post)

            await self._call_handle(self.before_query, info)
            record = await self._sql.select_one(info)

            if record:
                records = [record]
                values.bind(self, A.WRITE, records)
                logger.debug('update record(s): %s' % values)
                await self._call_handle(self.before_update, raw_post, values, records)
                await self._sql.update(records, values)
                await self._call_handle(self.after_update, raw_post, values, records)
                if values.returning:
                    await self.check_records_permission(None, records)
                    self.finish(RETCODE.SUCCESS, records)
                else:
                    self.finish(RETCODE.SUCCESS, len(records))
            else:
                self.finish(RETCODE.NOT_FOUND)
    set = update

    async def new(self):
        with ErrorCatchContext(self):
            raw_post = await self.post_data()
            values = SQLValuesToWrite(raw_post)
            values_lst = [values]

            logger.debug('insert record(s): %s' % values_lst)
            await self._call_handle(self.before_insert, raw_post, values_lst)
            records = await self._sql.insert(values_lst, returning=True)
            await self._call_handle(self.after_insert, raw_post, values_lst, records)
            if values.returning:
                await self.check_records_permission(None, records)
                self.finish(RETCODE.SUCCESS, records)
            else:
                self.finish(RETCODE.SUCCESS, len(records))

    async def delete(self):
        with ErrorCatchContext(self):
            info = SQLQueryInfo(self.params, self)
            await self._call_handle(self.before_query, info)
            record = await self._sql.select_one(info)

            if record:
                records = [record]
                logger.debug('request permission: [%s] of table %r' % (A.DELETE, self.table_name))
                for record in records:
                    valid = self.ability.can_with_record(self.current_user, A.DELETE, record, available=record.keys())

                    if len(valid) == len(record.keys()):
                        logger.debug("request permission successed: %r" % list(record.keys()))
                    else:
                        logger.debug(
                            "request permission failed. valid / requested: %r, %r" % (valid, list(record.keys())))
                        return self.finish(RETCODE.PERMISSION_DENIED)

                await self._call_handle(self.before_delete, records)
                await self._sql.delete(records)
                await self._call_handle(self.after_delete, records)
            else:
                self.finish(RETCODE.NOT_FOUND)

    @staticmethod
    @abstractmethod
    async def _fetch_fields(cls):
        """
        4 values must be set up in this function:
        1. cls.table_name: str
        2. cls.primary_key: str
        3. cls.fields: Dict['column', SQL_TYPE]
        4. cls.foreign_keys: Dict['column', List[SQLForeignKey]]

        :param cls:
        :return:
        """
        pass

    async def before_query(self, info: SQLQueryInfo):
        pass

    async def after_read(self, records: List[DataRecord]):
        pass

    async def before_insert(self, raw_post: Dict, values: SQLValuesToWrite):
        pass

    async def after_insert(self, raw_post: Dict, values: SQLValuesToWrite, records: List[DataRecord]):
        """ Emitted before finish """
        pass

    async def before_update(self, raw_post: Dict, values: SQLValuesToWrite, records: List[DataRecord]):
        """ raw_post 权限过滤和列过滤前，values 过滤后 """
        pass

    async def after_update(self, raw_post: Dict, values: SQLValuesToWrite, records: List[DataRecord]):
        pass

    async def before_delete(self, records: List[DataRecord]):
        pass

    async def after_delete(self, deleted_records: List[DataRecord]):
        pass
