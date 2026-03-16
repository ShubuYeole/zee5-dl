"""
zee5.urls — ALL endpoints 100% confirmed from DefaultAuthProvidersRepository.smali
and DefaultNewUserRepository.java.

Auth endpoint pattern (all confirmed from smali const-string values):
    Base: getAuthBaseUrl() = "https://auth.zee5.com/"
    Path appended directly (no leading slash):

    sendOtp (phone + email):   auth.zee5.com/ + "v1/user/sendotp"
    verifyOTP (phone OTP):     auth.zee5.com/ + "v1/user/verifyotp"
    loginWithPhoneOtp:         auth.zee5.com/ + "v1/user/verifyotp"   ← same endpoint!
    loginWithEmailOtp:         auth.zee5.com/ + "v1/user/verifyotp"   ← same endpoint!
    loginWithEmailPassword:    auth.zee5.com/ + "v2/user/loginemail"
    loginWithAmazon:           auth.zee5.com/ + "v1/user/loginamazon"
    loginWithGoogle:           auth.zee5.com/ + "v1/user/logingoogle"  (from line 7351)
    registerWithAmazon:        auth.zee5.com/ + "v1/user/registeramazon"
    registerWithGoogle:        auth.zee5.com/ + "v1/user/registergoogle"
    renewAuthToken:            auth.zee5.com/ + "v1/user/renew"
    generateDeviceCode:        auth.zee5.com/ + "useraction/device/getcode"
    loginWithDeviceCode:       auth.zee5.com/ + "useraction/device/getdeviceuser"
    verifyOtpKidsPack:         auth.zee5.com/ + "v1/user/verifyOtpKidsPack"
    getUserToken:              auth.zee5.com/ + "v1/user/getusertoken"
"""

# ── Base URLs (confirmed from UrlProvider.java) ───────────────────────────
AUTH_BASE        = "https://auth.zee5.com/"           # ALL auth flows
NEW_USER_BASE    = "https://user.zee5.com/"           # user data, watchlist, settings
USERACTION_BASE  = "https://useraction.zee5.com"      # AuthProvidersAPI Retrofit base
                                                       # (but actual URL paths go to auth.zee5.com)
PROFILES_BASE    = "https://profiles.zee5.com"
DEVICE_BASE      = "https://subscriptionapi.zee5.com"
CATALOG_BASE     = "https://catalogapi.zee5.com"
CONTENT_BASE     = "https://contentapi.zee5.com"
CMS_BASE         = "https://gwapi.zee5.com"
SPAPI_BASE       = "https://spapi.zee5.com"
B2B_BASE         = "https://b2bapi.zee5.com"
GRAPH_QL_BASE    = "https://artemis.zee5.com/"
CERBERUS_BASE    = "https://cerberus.zee5.com/cerberus/"
COUNTRY_API      = "https://xtra.zee5.com/country"

# ── ✓ CONFIRMED auth endpoints (from DefaultAuthProvidersRepository.smali) ─
# All: AUTH_BASE + path  =  https://auth.zee5.com/{path}
# Note: paths have NO leading slash — base already ends with /

SEND_OTP          = AUTH_BASE + "v1/user/sendotp"
VERIFY_OTP        = AUTH_BASE + "v1/user/verifyotp"       # phone OTP + email OTP
LOGIN_EMAIL_PASS  = AUTH_BASE + "v2/user/loginemail"
LOGIN_AMAZON      = AUTH_BASE + "v1/user/loginamazon"
LOGIN_GOOGLE      = AUTH_BASE + "v1/user/logingoogle"
REGISTER_AMAZON   = AUTH_BASE + "v1/user/registeramazon"
REGISTER_GOOGLE   = AUTH_BASE + "v1/user/registergoogle"
TOKEN_REFRESH     = AUTH_BASE + "v1/user/renew"            # + ?refresh_token=...
DEVICE_CODE_GEN   = AUTH_BASE + "useraction/device/getcode"
DEVICE_CODE_LOGIN = AUTH_BASE + "useraction/device/getdeviceuser"
GET_USER_TOKEN    = AUTH_BASE + "v1/user/getusertoken"
VERIFY_OTP_KIDS   = AUTH_BASE + "v1/user/verifyOtpKidsPack"

# ── ✓ CONFIRMED user data endpoints (from DefaultNewUserRepository.java) ──
# All: NEW_USER_BASE + path  =  https://user.zee5.com/{path}

WATCHLIST_V1      = NEW_USER_BASE + "v1/watchlist"
WATCHLIST_V2      = NEW_USER_BASE + "v2/watchlist"         # default (non-B2B)
WATCH_HISTORY     = NEW_USER_BASE + "v1/watchhistory"
SETTINGS          = NEW_USER_BASE + "v1/settings"
CHANGE_PHONE      = NEW_USER_BASE + "v1/user/changePhoneNumber"

# ── ✓ CONFIRMED profile endpoints ─────────────────────────────────────────
PROFILES_V2        = PROFILES_BASE + "/v2/profiles"
PROFILES_V2_BY_ID  = PROFILES_BASE + "/v2/profiles/{profile_id}"

# ── ✓ CONFIRMED device registration (RegisterDeviceUseCase.smali) ─────────
DEVICE_REGISTER    = DEVICE_BASE + "/v1/device"

# ── Playback ──────────────────────────────────────────────────────────────
SINGLE_PLAYBACK    = SPAPI_BASE + "/singlePlayback/v2/getDetails/secure"
TRAILER            = CMS_BASE   + "/content/trailer"
GRAPH_QL_FULL      = GRAPH_QL_BASE + "artemis/graphql"

# ── Confirmed constants ────────────────────────────────────────────────────
# Android TV APK key (UrlProvider.java)
ESK_KEY_TV          = "HOBNPuy7H3T5meJJAfyLkJlHaX2dXeEB"
# Web key — decoded from sniffed esk header:
# Base64("deviceId__gBQaZLiNdGN9UsCKZaloghz9t9StWLSD__timestamp")
ESK_KEY_WEB         = "gBQaZLiNdGN9UsCKZaloghz9t9StWLSD"
ESK_KEY             = ESK_KEY_WEB   # auth.zee5.com uses web key
DEFAULT_GUEST_TOKEN = "8ac71050855811eb9365c7b9492c1290"
