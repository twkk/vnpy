# encoding: UTF-8

from __future__ import division
import json
import shelve
import os
import traceback
from collections import OrderedDict

from vnpy.event import Event
from vnpy.trader.vtEvent import (EVENT_TICK, EVENT_TRADE, EVENT_CONTRACT,
                                 EVENT_ORDER)
from vnpy.trader.vtFunction import getTempPath, getJsonPath
from vnpy.trader.vtObject import (VtLogData, VtSubscribeReq, 
                                  VtOrderReq, VtCancelOrderReq)
from vnpy.trader.vtConstant import (PRODUCT_OPTION, OPTION_CALL, OPTION_PUT,
                                    DIRECTION_LONG, DIRECTION_SHORT,
                                    OFFSET_OPEN, OFFSET_CLOSE,
                                    PRICETYPE_LIMITPRICE)
from vnpy.pricing import black, bs, crr

from .omBase import (OmOption, OmUnderlying, OmChain, OmPortfolio,
                     EVENT_OM_LOG,
                     OM_DB_NAME)



# 定价模型字典
MODEL_DICT = {}
MODEL_DICT['black'] = black
MODEL_DICT['bs'] = bs
MODEL_DICT['crr'] = crr




########################################################################
class OmEngine(object):
    """期权主引擎"""
    impvFileName = 'PricingImpv.vt'
    impvFilePath = getTempPath(impvFileName)
    
    #----------------------------------------------------------------------
    def __init__(self, mainEngine, eventEngine):
        """Constructor"""
        self.mainEngine = mainEngine
        self.eventEngine = eventEngine
        
        self.portfolio = None
        self.optionContractDict = {}      # symbol:contract
        
        self.registerEvent()
    
    #----------------------------------------------------------------------
    def registerEvent(self):
        """注册事件监听"""
        self.eventEngine.register(EVENT_CONTRACT, self.processContractEvent)
    
    #----------------------------------------------------------------------
    def processTickEvent(self, event):
        """行情事件"""
        tick = event.dict_['data']
        self.portfolio.newTick(tick)
    
    #----------------------------------------------------------------------
    def processTradeEvent(self, event):
        """成交事件"""
        trade = event.dict_['data']
        self.portfolio.newTrade(trade)
    
    #----------------------------------------------------------------------
    def processContractEvent(self, event):
        """合约事件"""
        contract = event.dict_['data']
        if contract.symbol and contract.productClass == PRODUCT_OPTION:
            self.optionContractDict[contract.symbol] = contract
    
    #----------------------------------------------------------------------
    def subscribeEvent(self, symbol):
        """订阅对应合约的事件"""
        contract = self.mainEngine.getContract(symbol)
        if not contract:
            self.writeLog(u'行情订阅失败，找不到合约：%s' %symbol)
            return
            
        vtSymbol = contract.vtSymbol
        
        # 订阅行情
        req = VtSubscribeReq()
        req.symbol = contract.symbol
        req.exchange = contract.exchange
        self.mainEngine.subscribe(req, contract.gatewayName)
        
        # 订阅事件
        self.eventEngine.register(EVENT_TICK + vtSymbol, self.processTickEvent)
        self.eventEngine.register(EVENT_TRADE + vtSymbol, self.processTradeEvent)
    
    #----------------------------------------------------------------------
    def initEngine(self, fileName):
        """初始化引擎"""
        if self.portfolio:
            return False
        
        f = file(fileName)
        setting = json.load(f)
        
        # 读取定价模型
        model = MODEL_DICT.get(setting['model'], None)
        if not model:
            self.writeLog(u'找不到定价模型%s' %setting['model'])
            return
            
        # 创建标的对象
        underlyingDict = OrderedDict()
        
        for underlyingSymbol in setting['underlying']:
            contract = self.mainEngine.getContract(underlyingSymbol)
            if not contract:
                self.writeLog(u'找不到标的物合约%s' %underlyingSymbol)
                continue
            
            detail = self.mainEngine.getPositionDetail(contract.vtSymbol)
            
            underlying = OmUnderlying(contract, detail)
            underlyingDict[underlyingSymbol] = underlying
        
        # 创建期权链对象并初始化
        chainList = []
        
        for d in setting['chain']:
            chainSymbol = d['chainSymbol']
            r = d['r']
            
            # 锁定标的对象
            underlying = underlyingDict.get(d['underlyingSymbol'], None)
            if not underlying:
                self.writeLog(u'%s期权链的标的合约%s尚未创建，请检查配置文件' %(chainSymbol, underlyingSymbol))
                continue
            
            # 创建期权对象并初始化
            callDict = {}
            putDict = {}
            
            for symbol, contract in self.optionContractDict.items():
                if contract.underlyingSymbol == d['chainSymbol']:
                    detail = self.mainEngine.getPositionDetail(contract.vtSymbol)
                    option = OmOption(contract, detail, underlying, model, r)
                    
                    if contract.optionType is OPTION_CALL:
                        callDict[option.k] = option
                    else:
                        putDict[option.k] = option
                        
            # 期权排序
            strikeList = callDict.keys()
            strikeList.sort()
            callList = [callDict[k] for k in strikeList]
            putList = [putDict[k] for k in strikeList]
            
            # 创建期权链
            chain = OmChain(chainSymbol, callList, putList)
            chainList.append(chain)
            
            # 添加标的映射关系
            underlying.addChain(chain)

        # 创建持仓组合对象并初始化
        self.portfolio = OmPortfolio(setting['name'], model, underlyingDict.values(), chainList)
        
        # 载入波动率配置
        self.loadImpvSetting()
        
        # 订阅行情和事件
        for underlying in underlyingDict.values():
            self.subscribeEvent(underlying.vtSymbol)
            
        for chain in chainList:
            for option in chain.optionDict.values():
                self.subscribeEvent(option.vtSymbol)
        
        # 载入成功返回
        return True
        
    #----------------------------------------------------------------------
    def loadImpvSetting(self):
        """载入波动率配置"""
        f = shelve.open(self.impvFilePath)
        
        for chain in self.portfolio.chainDict.values():
            for option in chain.optionDict.values():
                option.pricingImpv = f.get(option.symbol, 0)
        
        f.close()
    
    #----------------------------------------------------------------------
    def saveImpvSetting(self):
        """保存波动率配置"""
        if not self.portfolio:
            return
        
        f = shelve.open(self.impvFilePath)
        
        for chain in self.portfolio.chainDict.values():
            for option in chain.optionDict.values():
                f[option.symbol] = option.pricingImpv
        
        f.close()
            
    #----------------------------------------------------------------------
    def stop(self):
        """关闭函数"""
        self.saveImpvSetting()
        
    #----------------------------------------------------------------------
    def writeLog(self, content):
        """发出日志 """
        log = VtLogData()
        log.logContent = content
        
        event = Event(EVENT_OM_LOG)
        event.dict_['data'] = log
        self.eventEngine.put(event)     


########################################################################
class OmStrategyEngine(object):
    """策略引擎"""

    #----------------------------------------------------------------------
    def __init__(self, omEngine, eventEngine):
        """Constructor"""
        self.omEngine = omEngine
        self.mainEngine = omEngine.mainEngine
        self.eventEngine = eventEngine
        
        self.portfolio = None
        
        self.strategyDict = {}          # name: strategy
        self.symbolStrategyDict = {}    # vtSymbol：strategy list
        self.orderStrategyDict= {}      # vtOrderID: strategy
    
    #----------------------------------------------------------------------
    def registerEvent(self):
        """注册事件监听"""
        self.eventEngine.register(EVENT_TICK, self.processTickEvent)
        self.eventEngine.register(EVENT_TRADE, self.processTradeEvent)
        self.eventEngine.register(EVENT_ORDER, self.processOrdervent)
    
    #----------------------------------------------------------------------
    def writeLog(self, content):
        """快速发出日志事件"""
        self.omEngine.writeLog(content)
        
    #----------------------------------------------------------------------
    def callStrategyFunc(self, strategy, func, params=None):
        """调用策略的函数，若触发异常则捕捉"""
        try:
            if params:
                func(params)
            else:
                func()
        except Exception:
            # 停止策略，修改状态为未初始化
            strategy.trading = False
            strategy.inited = False
            
            # 发出日志
            content = '\n'.join([u'策略%s触发异常已停止' %strategy.name,
                                traceback.format_exc()])
            self.writeLog(content)    
    
    #----------------------------------------------------------------------
    def processTickEvent(self, event):
        """处理行情事件"""
        tick = event.dict_['data']
        l = self.symbolStrategyDict.get(tick.vtSymbol, None)
        if l:
            for strategy in l:
                self.callStrategyFunc(strategy, strategy.onTick, tick)
    
    #----------------------------------------------------------------------
    def processTradeEvent(self, event):
        """处理成交事件"""
        trade = event.dict_['data']
        strategy = self.orderStrategyDict.get(trade.vtOrderID, None)
        if strategy:
            self.callStrategyFunc(strategy, strategy.onTrade, trade)
    
    #----------------------------------------------------------------------
    def processOrderEvent(self, event):
        """处理委托事件"""
        order = event.dict_['data']
        strategy = self.orderStrategyDict.get(order.vtOrderID, None)
        if strategy:
            self.callStrategyFunc(strategy, strategy.onOrder, order)
            
    #----------------------------------------------------------------------
    def loadSetting(self):
        """加载配置"""
        self.portfolio = self.omEngine.portfolio
    
    #----------------------------------------------------------------------
    def initStrategy(self, name):
        """初始化策略"""
        strategy = self.strategyDict[name]
        self.callStrategyFunc(strategy, strategy.onInit)
    
    #----------------------------------------------------------------------
    def startStrategy(self, name):
        """启动策略"""
        strategy = self.strategyDict[name]
        self.callStrategyFunc(strategy, strategy.onStart)        
   
    #----------------------------------------------------------------------
    def stopStrategy(self, name):
        """停止策略"""
        strategy = self.strategyDict[name]
        self.callStrategyFunc(strategy, strategy.onStop)
    
    #----------------------------------------------------------------------
    def sendOrder(self, vtSymbol, direction, offset, price, volume):
        """发单"""
        contract = self.mainEngine.getContract(vtSymbol)
        if not contract:
            return ''
        
        req = VtOrderReq()
        req.symbol = contract.symbol
        req.exchange = contract.exchange
        req.vtSymbol = vtSymbol
        req.price = price
        req.volume = volume
        req.direction = direction
        req.offset = offset
        req.priceType = PRICETYPE_LIMITPRICE
        
        return self.mainEngine.sendOrder(req, contract.gatewayName)
    
    #----------------------------------------------------------------------
    def cancelorder(self, vtOrderID):
        """撤单"""
        order = self.mainEngine.getOrder(vtOrderID)
        if not order:
            return
        
        req = VtCancelOrderReq()
        req.symbol = order.symbol
        req.exchange = order.exchange
        req.orderID = order.orderID
        req.symbol = order.symbol
        req.vtSymbol = order.vtSymbo
        
        self.mainEngine.cancelOrder(req, order.gatewayName)
    
    #----------------------------------------------------------------------
    def buy(self, vtSymbol, price, volume):
        """开多"""
        return self.sendOrder(vtSymbol, DIRECTION_LONG, OFFSET_OPEN, price, volume)
        
    #----------------------------------------------------------------------
    def short(self, vtSymbol, price, volume):
        """开空"""
        return self.sendOrder(vtSymbol, DIRECTION_SHORT, OFFSET_OPEN, price, volume)
    
    #----------------------------------------------------------------------
    def sell(self, vtSymbol, price, volume):
        """平多"""
        return self.sendOrder(vtSymbol, DIRECTION_SHORT, OFFSET_CLOSE, price, volume)
            
    #----------------------------------------------------------------------
    def cover(self, vtSymbol, price, volume):
        """平空"""
        return self.sendOrder(vtSymbol, DIRECTION_LONG, OFFSET_CLOSE, price, volume)
    
    #----------------------------------------------------------------------
    def dbQuery(self, collectionName, flt):
        """查询数据"""
        return self.mainEngine.dbQuery(OM_DB_NAME, collectionName, flt)
    
    #----------------------------------------------------------------------
    def dbUpdate(self, collectionName, d, flt):
        """更新数据"""
        self.mainEngine.dbUpdate(OM_DB_NAME, collectionName, d, flt, True)
        
    #----------------------------------------------------------------------
    def getOption(self, vtSymbol):
        """获取期权信息"""
        return self.portfolio.optionDict.get(vtSymbol, None)
        
    #----------------------------------------------------------------------
    def getUnderlying(self, vtSymbol):
        """获取标的信息"""
        return self.portfolio.underlyingDict.get(vtSymbol, None)
    
    #----------------------------------------------------------------------
    def getChain(self, symbol):
        """获取期权链信息"""
        return self.portfolio.chainDict.get(symbol, None)
    
    #----------------------------------------------------------------------
    def getPortfolio(self):
        """获取持仓组合信息"""
        return self.portfolio
        
    
