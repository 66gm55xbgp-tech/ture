//+------------------------------------------------------------------+
//| HybridGB_Bridge_EA.mq5                                           |
//| Hybrid-GB MT5 Bridge EA v3.15                                    |
//|                                                                  |
//| Pull model (unchanged): on every tick the EA POSTs its state to  |
//| ServerURL + "/state" and executes the command in the response.   |
//|                                                                  |
//| v3.00 (2026-08-19) - platform integration:                       |
//| v3.10 (2026-08-21) - STOP orders (exchange-enforced trail/loss   |
//|   stops), CANCEL_TICKET, contract_size in register payload       |
//|   - AuthToken input -> "X-GB-Auth" header on EVERY request        |
//|   - POST /register on init + every 5 min: account details        |
//|     (login/server/company/currency/leverage/trade_mode) and the  |
//|     full tradeable symbol list -> powers the dashboard MT5 tab    |
//|   - Binance-like order styling: OPEN_BUY_LIMIT / OPEN_SELL_LIMIT |
//|     (resting ladder orders), CANCEL_ALL, plus market orders      |
//|   - exec echo: every executed command is reported back in the    |
//|     next state POST ("exec": {command_id, retcode, ticket})      |
//|   - pending orders included in the payload ("orders_data")       |
//|   - filling-mode auto-detect (IOC/FOK/RETURN)                    |
//|                                                                  |
//| Parameters:                                                      |
//|   ServerURL    - http://HOST:PORT/api/mt5   (no trailing slash)  |
//|   AuthToken    - the MT5 token from Dashboard > Settings         |
//|   MagicNumber  - isolate this EA's positions from others         |
//|   LotSize      - fallback lot when the command sends volume=0    |
//|   LocalHardSL  - fallback SL distance if server sends sl=0       |
//|   BarsToSend   - OHLCV bars per timeframe                        |
//|   TickThrottle - min ms between state POSTs (0 = every tick)     |
//| v3.14 — pending LIMIT/STOP use ORDER_FILLING_RETURN (IOC was     |
//|   expiring GTC ladder so Python showed SELL and MT5 did not).    |
//| v3.13 — if no HTTP 200 for ServerLostSec (default 90s), EA       |
//|   flattens this chart locally. Also flattens on EA/chart/terminal|
//|   stop. CLOSE_CHART closes any magic on the symbol.              |
//+------------------------------------------------------------------+
#property copyright "Hybrid-GB"
#property version   "3.15"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\OrderInfo.mqh>

// -- Inputs --------------------------------------------------------
input string  ServerURL    = "http://34.146.199.62:9100/api/mt5"; // Server URL (no slash at end)
input string  AuthToken    = "";                            // MT5 token (Dashboard > Settings)
input int     MagicNumber  = 888888;                        // Magic number
input double  LotSize      = 0.01;                          // Fallback lot per order
input double  LocalHardSL  = 150.0;                         // Fallback SL (price units)
input int     BarsToSend   = 50;                            // OHLCV bars per timeframe
input int     TickThrottle = 250;                           // Min ms between state POSTs
input int     ServerLostSec = 90;                           // No HTTP 200 for this many seconds -> flatten this chart
input bool    VerboseLog   = false;                         // Extra debug logging

// -- Globals -------------------------------------------------------
CTrade        Trade;
CPositionInfo PosInfo;
COrderInfo    OrdInfo;

string  g_symbol;
int     g_digits;
double  g_point;
long    g_login        = 0;
string  g_lastCmdId    = "";
string  g_execJson     = "";      // exec echo carried on the next POST
string  g_stateURL;
string  g_registerURL;
ulong   g_lastPostMs   = 0;
ulong   g_lastRegMs    = 0;
ulong   g_lastOkMs     = 0;       // last HTTP 200 from /state
bool    g_lostFlat     = false;   // already auto-flattened this outage
int     g_tickCount    = 0;
ENUM_ORDER_TYPE_FILLING g_filling = ORDER_FILLING_IOC;

// -- Logging helper ------------------------------------------------
void Log(string msg, bool force = false)
{
   if(VerboseLog || force)
      Print("[HybridGB][", g_symbol, "] ", msg);
}

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   g_symbol  = Symbol();
   g_digits  = (int)SymbolInfoInteger(g_symbol, SYMBOL_DIGITS);
   g_point   = SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   g_login   = AccountInfoInteger(ACCOUNT_LOGIN);
   g_stateURL    = ServerURL + "/state";
   g_registerURL = ServerURL + "/register";

   Trade.SetExpertMagicNumber(MagicNumber);
   Trade.SetDeviationInPoints(20);
   _DetectFilling();

   Print("+==========================================+");
   Print("|   HybridGB Bridge EA v3.15               |");
   Print("+------------------------------------------+");
   Print("| Symbol:  ", g_symbol);
   Print("| Server:  ", g_stateURL);
   Print("| Token:   ", StringLen(AuthToken) > 0 ? "(set)" : "(MISSING!)");
   Print("| Magic:   ", MagicNumber);
   Print("+==========================================+");
   Print("[WARN] Add '", ServerURL, "' to: Tools > Options > Expert Advisors > Allow WebRequest");

   if(StringLen(AuthToken) == 0)
      Print("[HybridGB] [FAIL] AuthToken is EMPTY - set it from Dashboard > Settings > MT5");

   EventSetTimer(1);
   g_lastOkMs = GetTickCount64();        // grace: don't flatten on a slow first POST
   SendRegister();                       // account + symbols right away
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   // Terminal/chart/EA going away — flatten locally; the server cannot
   // push closes if we are no longer POSTing.
   if(reason == REASON_REMOVE || reason == REASON_CHARTCLOSE
      || reason == REASON_CLOSE)
   {
      int n = _CloseChart();
      Print("[HybridGB] EA stopping (reason=", reason,
            ") — closed/cancelled ", n, " on ", g_symbol);
   }
   Print("[HybridGB] EA stopped (reason=", reason, ")");
}

//+------------------------------------------------------------------+
//| Filling mode auto-detect                                         |
//+------------------------------------------------------------------+
void _DetectFilling()
{
   long modes = SymbolInfoInteger(g_symbol, SYMBOL_FILLING_MODE);
   if((modes & SYMBOL_FILLING_IOC) != 0)      g_filling = ORDER_FILLING_IOC;
   else if((modes & SYMBOL_FILLING_FOK) != 0) g_filling = ORDER_FILLING_FOK;
   else                                       g_filling = ORDER_FILLING_RETURN;
   Trade.SetTypeFilling(g_filling);
   Log("filling mode = " + (string)g_filling, true);
}

//+------------------------------------------------------------------+
//| Timers                                                           |
//+------------------------------------------------------------------+
void OnTick()
{
   ulong now = GetTickCount64();
   if(TickThrottle > 0 && (now - g_lastPostMs) < (ulong)TickThrottle)
      return;
   g_lastPostMs = now;
   g_tickCount++;
   SendState();
}

void OnTimer()
{
   ulong now = GetTickCount64();
   // 5 s without a tick -> send state anyway (slow symbols / weekend)
   if((now - g_lastPostMs) >= 5000)
   {
      g_lastPostMs = now;
      SendState();
   }
   // re-register every 5 min (account details + symbol list refresh)
   if((now - g_lastRegMs) >= 300000)
   {
      g_lastRegMs = now;
      SendRegister();
   }
   _CheckServerLost();
}

//+------------------------------------------------------------------+
//| Common headers (auth)                                            |
//+------------------------------------------------------------------+
string _Headers()
{
   return "Content-Type: application/json\r\n" +
          "X-GB-Auth: " + AuthToken + "\r\n";
}

int _Post(string url, string json, string &response)
{
   uchar  postData[];
   uchar  resultData[];
   string resultHeaders;
   StringToCharArray(json, postData, 0, StringLen(json));
   int rc = WebRequest("POST", url, _Headers(), 5000,
                       postData, resultData, resultHeaders);
   if(rc == 200)
      response = CharArrayToString(resultData);
   return rc;
}

//+------------------------------------------------------------------+
//| REGISTER - account details + full symbol list                    |
//+------------------------------------------------------------------+
void SendRegister()
{
   g_lastRegMs = GetTickCount64();
   if(StringLen(AuthToken) == 0) return;

   string tradeMode = "demo";
   long tm = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(tm == ACCOUNT_TRADE_MODE_REAL)  tradeMode = "live";
   // ACCOUNT_TRADE_MODE_DEMO / CONTEST -> demo

   string acc = "{"
      + "\"login\":"        + (string)g_login + ","
      + "\"name\":\""       + AccountInfoString(ACCOUNT_NAME) + "\","
      + "\"server\":\""     + AccountInfoString(ACCOUNT_SERVER) + "\","
      + "\"company\":\""    + AccountInfoString(ACCOUNT_COMPANY) + "\","
      + "\"currency\":\""   + AccountInfoString(ACCOUNT_CURRENCY) + "\","
      + "\"leverage\":"     + (string)AccountInfoInteger(ACCOUNT_LEVERAGE) + ","
      + "\"trade_mode\":\"" + tradeMode + "\","
      + "\"balance\":"      + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ","
      + "\"equity\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2)
      + "}";

   // tradeable symbol list (visible + trade-enabled)
   int total  = SymbolsTotal(false);
   int listed = 0;
   string arr = "[";
   for(int i = 0; i < total && listed < 400; i++)
   {
      string s = SymbolName(i, false);
      if(!SymbolInfoInteger(s, SYMBOL_SELECT)) continue;
      if((int)SymbolInfoInteger(s, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL) continue;
      long digits = SymbolInfoInteger(s, SYMBOL_DIGITS);
      double pt   = SymbolInfoDouble(s, SYMBOL_POINT);
      if(pt <= 0) continue;
      if(listed > 0) arr += ",";
      listed++;
      arr += "{\"name\":\"" + s + "\""
           + ",\"digits\":"  + (string)digits
           + ",\"point\":"   + DoubleToString(pt, (int)digits)
           + ",\"contract_size\":" + DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_CONTRACT_SIZE), 2)
           + ",\"lot_min\":" + DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MIN), 2)
           + ",\"lot_max\":" + DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MAX), 2)
           + ",\"lot_step\":" + DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_STEP), 3)
           + ",\"stops_level\":" + (string)SymbolInfoInteger(s, SYMBOL_TRADE_STOPS_LEVEL)
           + "}";
   }
   arr += "]";

   string json = "{\"account\":" + acc + ",\"chart_symbol\":\"" + g_symbol
               + "\",\"magic\":" + (string)MagicNumber
               + ",\"symbols\":" + arr + "}";

   string response = "";
   int rc = _Post(g_registerURL, json, response);
   if(rc == 200)
      Print("[HybridGB] registered [OK] (", listed, " symbols) -> ", response);
   else if(rc == 401 || rc == 403)
      Print("[HybridGB] [FAIL] register REJECTED (HTTP ", rc, ") - check AuthToken");
   else
      Log("register failed HTTP " + (string)rc, true);
}

//+------------------------------------------------------------------+
//| STATE - send everything, execute the returned command             |
//+------------------------------------------------------------------+
void SendState()
{
   double bid    = SymbolInfoDouble(g_symbol, SYMBOL_BID);
   double ask    = SymbolInfoDouble(g_symbol, SYMBOL_ASK);
   double spread = (ask - bid) / g_point;

   int    posCount  = 0;
   double basketPnl = 0.0;
   string posArr    = "";
   bool   firstPos  = true;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!PosInfo.SelectByIndex(i)) continue;
      if(PosInfo.Symbol() != g_symbol) continue;

      long   ticket    = (long)PosInfo.Ticket();
      int    posType   = (int)PosInfo.PositionType();
      double vol       = PosInfo.Volume();
      double openPrice = PosInfo.PriceOpen();
      double profit    = PosInfo.Profit();
      double sl        = PosInfo.StopLoss();
      long   openTime  = (long)PosInfo.Time();
      string typStr    = (posType == POSITION_TYPE_BUY) ? "BUY" : "SELL";

      basketPnl += profit;
      posCount++;

      if(!firstPos) posArr += ",";
      firstPos = false;
      posArr += "{"
             + "\"ticket\":"      + (string)ticket
             + ",\"type\":\""     + typStr + "\""
             + ",\"volume\":"     + DoubleToString(vol, 2)
             + ",\"price_open\":" + DoubleToString(openPrice, g_digits)
             + ",\"profit\":"     + DoubleToString(profit, 2)
             + ",\"sl\":"         + DoubleToString(sl, g_digits)
             + ",\"time\":"       + (string)openTime
             + "}";
   }

   // pending orders (magic + symbol filtered)
   int    ordCount = 0;
   string ordArr   = "";
   bool   firstOrd = true;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrdInfo.SelectByIndex(i)) continue;
      if(OrdInfo.Symbol() != g_symbol) continue;

      ulong  t      = OrdInfo.Ticket();
      long   type   = (long)OrdInfo.OrderType();
      double ovol   = OrdInfo.VolumeCurrent();
      double oprice = OrdInfo.PriceOpen();
      string tstr   = "LIMIT";
      if(type == ORDER_TYPE_BUY_STOP || type == ORDER_TYPE_SELL_STOP)  tstr = "STOP";
      if(type == ORDER_TYPE_BUY_STOP_LIMIT || type == ORDER_TYPE_SELL_STOP_LIMIT) tstr = "STOP_LIMIT";
      string oside  = (type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_BUY_STOP
                       || type == ORDER_TYPE_BUY_STOP_LIMIT) ? "BUY" : "SELL";

      ordCount++;
      if(!firstOrd) ordArr += ",";
      firstOrd = false;
      ordArr += "{"
             + "\"ticket\":"     + (string)t
             + ",\"type\":\""    + tstr + "\""
             + ",\"side\":\""    + oside + "\""
             + ",\"volume\":"    + DoubleToString(ovol, 2)
             + ",\"price\":"     + DoubleToString(oprice, g_digits)
             + "}";
   }

   double atr = _CalcATR(14);
   double rsi = _CalcRSI(14);

   string barsM1  = _BuildBarsJson(PERIOD_M1,  BarsToSend);
   string barsM5  = _BuildBarsJson(PERIOD_M5,  BarsToSend);
   string barsM15 = _BuildBarsJson(PERIOD_M15, BarsToSend);
   string barsH1  = _BuildBarsJson(PERIOD_H1,  BarsToSend);

   string execPart = (StringLen(g_execJson) > 0) ? ",\"exec\":" + g_execJson : "";

   string json = "{"
              + "\"symbol\":\""     + g_symbol + "\","
              + "\"magic\":"        + (string)MagicNumber + ","
              + "\"bid\":"          + DoubleToString(bid, g_digits) + ","
              + "\"ask\":"          + DoubleToString(ask, g_digits) + ","
              + "\"spread\":"       + DoubleToString(spread, 1) + ","
              + "\"equity\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ","
              + "\"balance\":"      + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ","
              + "\"freeMargin\":"   + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ","
              + "\"positions\":"    + (string)posCount + ","
              + "\"basket_pnl\":"   + DoubleToString(basketPnl, 2) + ","
              + "\"atr\":"          + DoubleToString(atr, g_digits) + ","
              + "\"rsi\":"          + DoubleToString(rsi, 2) + ","
              + "\"positions_data\":[" + posArr + "],"
              + "\"orders_data\":[" + ordArr + "]"
              + ",\"mt5_positions_total\":" + (string)PositionsTotal()
              + ",\"mt5_orders_total\":" + (string)OrdersTotal()
              + execPart
              + ",\"bars_m1\":"     + barsM1
              + ",\"bars_m5\":"     + barsM5
              + ",\"bars_m15\":"    + barsM15
              + ",\"bars_h1\":"     + barsH1
              + "}";

   string response = "";
   int rc = _Post(g_stateURL, json, response);
   g_execJson = "";                       // echo consumed (sent once)

   if(rc < 0)
   {
      int err = GetLastError();
      if(err == 4014)
         Print("[HybridGB] [WARN] URL not in allowed list: ", g_stateURL,
               " -> Tools > Options > Expert Advisors > Allow WebRequest");
      else
         Log("POST failed err=" + (string)err, true);
      _CheckServerLost();
      return;
   }
   if(rc == 401 || rc == 403)
   {
      Print("[HybridGB] [FAIL] state REJECTED (HTTP ", rc, ") - AuthToken mismatch; re-registering");
      SendRegister();
      _CheckServerLost();
      return;
   }
   if(rc != 200)
   {
      Log("Server returned HTTP " + (string)rc, true);
      _CheckServerLost();
      return;
   }

   g_lastOkMs = GetTickCount64();
   g_lostFlat = false;
   Log(" " + response);

   // -- Parse command ---------------------------------------------
   string action  = _JsonStr(response, "action");
   string cmdId   = _JsonStr(response, "command_id");
   double volume  = _JsonNum(response, "volume");
   double sl_val  = _JsonNum(response, "sl");
   double price   = _JsonNum(response, "price");

   if(volume <= 0) volume = LotSize;

   if(cmdId != "" && cmdId == g_lastCmdId)
      return;
   if(cmdId != "") g_lastCmdId = cmdId;

   if(action == "OPEN_BUY" || action == "OPEN_SELL")
   {
      _ExecMarket(action == "OPEN_BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL,
                  volume, sl_val, cmdId);
   }
   else if(action == "OPEN_BUY_LIMIT" || action == "OPEN_SELL_LIMIT")
   {
      _ExecLimit(action == "OPEN_BUY_LIMIT" ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT,
                 volume, price, cmdId);
   }
   else if(action == "OPEN_BUY_STOP" || action == "OPEN_SELL_STOP")
   {
      _ExecStop(action == "OPEN_BUY_STOP" ? ORDER_TYPE_BUY_STOP : ORDER_TYPE_SELL_STOP,
                volume, price, cmdId);
   }
   else if(action == "CLOSE_ALL")
   {
      int closed = _CloseAll();
      _QueueExec(cmdId, closed > 0 ? 10009 : 10013, 0);   // TRADE_RETCODE_DONE : not found
      Print("[HybridGB] CLOSE_ALL -> closed ", closed);
   }
   else if(action == "CLOSE_TICKET")
   {
      long ticket = (long)_JsonNum(response, "ticket");
      bool ok = (ticket > 0) && _CloseTicket(ticket);
      _QueueExec(cmdId, ok ? 10009 : 10013, ticket);
   }
   else if(action == "CANCEL_TICKET")
   {
      long ticket = (long)_JsonNum(response, "ticket");
      bool ok = false;
      if(ticket > 0 && OrderSelect(ticket))
      {
         ok = Trade.OrderDelete((ulong)ticket);
      }
      _QueueExec(cmdId, ok ? 10009 : 10013, ticket);
      if(!ok) Print("[HybridGB] CANCEL_TICKET ", ticket, " failed/not found");
   }
   else if(action == "CANCEL_ALL")
   {
      int c = _CancelAll();
      _QueueExec(cmdId, 10009, 0);
      Print("[HybridGB] CANCEL_ALL -> cancelled ", c);
   }
   else if(action == "CLOSE_CHART")
   {
      int n = _CloseChart();
      _QueueExec(cmdId, n > 0 ? 10009 : 10013, 0);
      Print("[HybridGB] CLOSE_CHART -> ", n, " closed/cancelled (any magic)");
   }
   // NOOP: nothing to do
}

//+------------------------------------------------------------------+
//| Stash exec result -> carried on the next state POST                |
//+------------------------------------------------------------------+
void _QueueExec(string cmdId, int retcode, long ticket)
{
   if(StringLen(cmdId) == 0) return;
   g_execJson = "{\"command_id\":\"" + cmdId + "\""
              + ",\"retcode\":" + (string)retcode
              + ",\"ticket\":"  + (string)ticket + "}";
}

//+------------------------------------------------------------------+
//| Market order with hard SL safety net                              |
//+------------------------------------------------------------------+
void _ExecMarket(ENUM_ORDER_TYPE orderType, double vol, double serverSL, string cmdId)
{
   double ask   = SymbolInfoDouble(g_symbol, SYMBOL_ASK);
   double bid   = SymbolInfoDouble(g_symbol, SYMBOL_BID);
   double price = (orderType == ORDER_TYPE_BUY) ? ask : bid;

   double sl = 0.0;
   if(serverSL > 0.0)
   {
      sl = NormalizeDouble(serverSL, g_digits);
      if(orderType == ORDER_TYPE_BUY  && sl >= price)
         sl = NormalizeDouble(price - LocalHardSL * g_point * 10, g_digits);
      if(orderType == ORDER_TYPE_SELL && sl <= price)
         sl = NormalizeDouble(price + LocalHardSL * g_point * 10, g_digits);
   }

   bool ok = Trade.PositionOpen(g_symbol, orderType, vol, price, sl, 0,
                                "HybridGB " + cmdId);
   long ticket = (long)Trade.ResultOrder();
   _QueueExec(cmdId, ok ? 10009 : (int)Trade.ResultRetcode(), ticket);
   if(ok)
      Print("[HybridGB] OPENED ", EnumToString(orderType),
            " lot=", DoubleToString(vol, 2), " @ ", DoubleToString(price, g_digits),
            " ticket=", ticket);
   else
      Print("[HybridGB] OPEN FAILED retcode=", Trade.ResultRetcode(),
            " ", Trade.ResultComment());
}

//+------------------------------------------------------------------+
//| Pending LIMIT order (Binance-style resting ladder)                |
//+------------------------------------------------------------------+
void _ExecLimit(ENUM_ORDER_TYPE orderType, double vol, double price, string cmdId)
{
   if(price <= 0)
   {
      Log("LIMIT rejected: price=0", true);
      _QueueExec(cmdId, 10015, 0);       // TRADE_RETCODE_INVALID_PRICE
      return;
   }
   price = NormalizeDouble(price, g_digits);

   // Pending GTC limits MUST use RETURN. IOC (used for market fills) expires
   // a limit that does not fill immediately — Python logs success, MT5 shows nothing.
   Trade.SetTypeFilling(ORDER_FILLING_RETURN);
   bool ok = Trade.OrderOpen(g_symbol, orderType, vol, 0, price, 0, 0,
                             ORDER_TIME_GTC, 0, "HybridGB " + cmdId);
   Trade.SetTypeFilling(g_filling);
   long ticket = (long)Trade.ResultOrder();
   _QueueExec(cmdId, ok ? 10009 : (int)Trade.ResultRetcode(), ticket);
   if(ok)
      Print("[HybridGB] LIMIT ", EnumToString(orderType), " lot=",
            DoubleToString(vol, 2), " @ ", DoubleToString(price, g_digits),
            " ticket=", ticket);
   else
      Print("[HybridGB] LIMIT FAILED retcode=", Trade.ResultRetcode(),
            " ", Trade.ResultComment());
}

//+------------------------------------------------------------------+
//| Pending STOP order (exchange-enforced trail/loss stops)           |
//+------------------------------------------------------------------+
void _ExecStop(ENUM_ORDER_TYPE orderType, double vol, double price, string cmdId)
{
   if(price <= 0)
   {
      Log("STOP rejected: price=0", true);
      _QueueExec(cmdId, 10015, 0);
      return;
   }
   price = NormalizeDouble(price, g_digits);

   Trade.SetTypeFilling(ORDER_FILLING_RETURN);
   bool ok = Trade.OrderOpen(g_symbol, orderType, vol, 0, price, 0, 0,
                             ORDER_TIME_GTC, 0, "HybridGB " + cmdId);
   Trade.SetTypeFilling(g_filling);
   long ticket = (long)Trade.ResultOrder();
   _QueueExec(cmdId, ok ? 10009 : (int)Trade.ResultRetcode(), ticket);
   if(ok)
      Print("[HybridGB] STOP ", EnumToString(orderType), " lot=",
            DoubleToString(vol, 2), " @ ", DoubleToString(price, g_digits),
            " ticket=", ticket);
   else
      Print("[HybridGB] STOP FAILED retcode=", Trade.ResultRetcode(),
            " ", Trade.ResultComment());
}

//+------------------------------------------------------------------+
//| Close all positions for our magic + symbol                        |
//+------------------------------------------------------------------+
int _CloseAll()
{
   int closed = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!PositionGetTicket(i)) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_symbol) continue;
      ulong t = PositionGetInteger(POSITION_TICKET);
      if(Trade.PositionClose(t)) closed++;
   }
   return closed;
}

//+------------------------------------------------------------------+
//| Cancel all pending orders for our magic + symbol                  |
//+------------------------------------------------------------------+
int _CancelAll()
{
   int cancelled = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong t = OrderGetTicket(i);
      if(t == 0) continue;
      if((int)OrderGetInteger(ORDER_MAGIC) != MagicNumber) continue;
      if(OrderGetString(ORDER_SYMBOL) != g_symbol) continue;
      if(Trade.OrderDelete(t)) cancelled++;
   }
   return cancelled;
}

// Close EVERY position + pending order on this chart (any magic). Used when
// the server declares the EA link lost and the operator must go flat.
int _CloseChart()
{
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!PositionGetTicket(i)) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_symbol) continue;
      ulong t = (ulong)PositionGetInteger(POSITION_TICKET);
      if(Trade.PositionClose(t)) n++;
   }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong t = OrderGetTicket(i);
      if(t == 0) continue;
      if(OrderGetString(ORDER_SYMBOL) != g_symbol) continue;
      if(Trade.OrderDelete(t)) n++;
   }
   return n;
}

void _CheckServerLost()
{
   int lim = ServerLostSec;
   if(lim < 15) lim = 15;
   ulong now = GetTickCount64();
   if(g_lastOkMs == 0)
   {
      g_lastOkMs = now;
      return;
   }
   if((now - g_lastOkMs) < (ulong)lim * 1000)
      return;
   if(g_lostFlat)
      return;
   g_lostFlat = true;
   int n = _CloseChart();
   Print("[HybridGB] NO SERVER RESPONSE for ", lim,
         "s — auto-closed ", n, " position(s)/order(s) on ", g_symbol,
         ". Flatten leftovers in MT5 if any remain.");
}

//+------------------------------------------------------------------+
//| Close a specific ticket                                           |
//+------------------------------------------------------------------+
bool _CloseTicket(long ticket)
{
   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!PositionGetTicket(i)) continue;
      if(PositionGetInteger(POSITION_TICKET) != ticket) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

      if(Trade.PositionClose((ulong)ticket))
      {
         Print("[HybridGB] CLOSE_TICKET ", ticket, " -> OK");
         return true;
      }
      Print("[HybridGB] CLOSE_TICKET ", ticket, " FAILED ret=", Trade.ResultRetcode());
      return false;
   }
   Print("[HybridGB] CLOSE_TICKET ", ticket, " not found (already closed?)");
   return false;
}

//+------------------------------------------------------------------+
//| Build OHLCV JSON array for a timeframe                            |
//+------------------------------------------------------------------+
string _BuildBarsJson(ENUM_TIMEFRAMES tf, int count)
{
   MqlRates rates[];
   int copied = CopyRates(g_symbol, tf, 0, count, rates);
   if(copied <= 0) return "[]";

   string arr = "[";
   for(int i = 0; i < copied; i++)
   {
      if(i > 0) arr += ",";
      arr += "{\"t\":"  + (string)(long)rates[i].time
           + ",\"o\":" + DoubleToString(rates[i].open,  g_digits)
           + ",\"h\":" + DoubleToString(rates[i].high,  g_digits)
           + ",\"l\":" + DoubleToString(rates[i].low,   g_digits)
           + ",\"c\":" + DoubleToString(rates[i].close, g_digits)
           + ",\"v\":" + (string)rates[i].tick_volume + "}";
   }
   arr += "]";
   return arr;
}

//+------------------------------------------------------------------+
//| Simple ATR (True Range average)                                   |
//+------------------------------------------------------------------+
double _CalcATR(int period)
{
   MqlRates r[];
   if(CopyRates(g_symbol, PERIOD_M15, 0, period + 1, r) < period + 1) return 0;

   double sum = 0;
   for(int i = 1; i <= period; i++)
   {
      double tr = MathMax(r[i].high - r[i].low,
                  MathMax(MathAbs(r[i].high - r[i-1].close),
                          MathAbs(r[i].low  - r[i-1].close)));
      sum += tr;
   }
   return sum / period;
}

//+------------------------------------------------------------------+
//| Simple RSI                                                        |
//+------------------------------------------------------------------+
double _CalcRSI(int period)
{
   MqlRates r[];
   if(CopyRates(g_symbol, PERIOD_M15, 0, period + 2, r) < period + 2) return 50;

   double gain = 0, loss = 0;
   for(int i = 1; i <= period; i++)
   {
      double d = r[i].close - r[i-1].close;
      if(d > 0) gain += d; else loss -= d;
   }
   if(loss == 0) return 100;
   double rs = gain / loss;
   return 100 - 100 / (1 + rs);
}

//+------------------------------------------------------------------+
//| Minimal JSON field extractors                                     |
//+------------------------------------------------------------------+
string _JsonStr(string json, string key)
{
   string s = "\"" + key + "\":\"";
   int p = StringFind(json, s);
   if(p < 0) return "";
   p += StringLen(s);
   int e = StringFind(json, "\"", p);
   if(e < 0) return "";
   return StringSubstr(json, p, e - p);
}

double _JsonNum(string json, string key)
{
   string s = "\"" + key + "\":";
   int p = StringFind(json, s);
   if(p < 0) return 0.0;
   p += StringLen(s);
   while(p < StringLen(json) && StringSubstr(json, p, 1) == " ") p++;
   string num = "";
   while(p < StringLen(json))
   {
      string c = StringSubstr(json, p, 1);
      if(c == "," || c == "}" || c == "]" || c == " " || c == "\r" || c == "\n") break;
      num += c; p++;
   }
   return StringToDouble(num);
}
//+------------------------------------------------------------------+
