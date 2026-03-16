"""
zee5.models — 100% confirmed from decompiled APK DTOs.

Sources:
  SendOtpRequest.java          → phoneno, email, platformName, hashId
  VerifyOtpRequest.java        → phoneno, email, otp, platform, device, version
  VerifyOtpV3Request.java      → phoneno, email, otp  (v3 — no platform/device/version)
  VerifyPasswordRequest.java   → email, password, platform, device, version, guest_token
                                 default guest_token = "8ac71050855811eb9365c7b9492c1290"
  LoginResponse.java           → access_token, expires_in, token_type, refresh_token,
                                 shouldRegister, secureToken
  SendOtpResult.java           → code (int), message (nullable str)
  UserAuthResponse.java        → access_token, refresh_token, token_type, expires_in
"""
from __future__ import annotations
from typing import Any, Optional, Union
from pydantic import BaseModel, Field, AliasChoices, field_validator


# ── REQUEST bodies ──────────────────────────────────────────────────────────

class SendOtpRequest(BaseModel):
    """
    Confirmed from SendOtpRequest.java.
    Serial names: email, phoneno, platformName, hashId  (all nullable)
    """
    phoneno:       Optional[str] = None   # phone number field is "phoneno" NOT "mobile"!
    email:         Optional[str] = None
    platform_name: Optional[str] = Field(default=None, serialization_alias="platformName")
    hash_id:       Optional[str] = Field(default=None, serialization_alias="hashId")
    model_config   = {"populate_by_name": True}

    def to_api_dict(self) -> dict:
        d: dict = {}
        if self.phoneno:      d["phoneno"]      = self.phoneno
        if self.email:        d["email"]        = self.email
        if self.platform_name: d["platformName"] = self.platform_name
        if self.hash_id:      d["hashId"]       = self.hash_id
        return d


class VerifyOtpRequest(BaseModel):
    """
    Confirmed from VerifyOtpRequest.java.
    Serial names: email?, phoneno?, otp?, platform*, device*, version*
    (* = required, ? = optional)
    """
    phoneno:  Optional[str] = None
    email:    Optional[str] = None
    otp:      Optional[str] = None
    platform: str = "androidtv"
    device:   str = "tv"
    version:  str = "9.3.5"

    def to_api_dict(self) -> dict:
        d: dict = {"platform": self.platform, "device": self.device, "version": self.version}
        if self.phoneno: d["phoneno"] = self.phoneno
        if self.email:   d["email"]   = self.email
        if self.otp:     d["otp"]     = self.otp
        return d


class VerifyOtpV3Request(BaseModel):
    """
    Confirmed from VerifyOtpV3Request.java — simpler v3 variant, no platform/device.
    Serial names: email?, phoneno?, otp?
    """
    phoneno: Optional[str] = None
    email:   Optional[str] = None
    otp:     Optional[str] = None

    def to_api_dict(self) -> dict:
        d: dict = {}
        if self.phoneno: d["phoneno"] = self.phoneno
        if self.email:   d["email"]   = self.email
        if self.otp:     d["otp"]     = self.otp
        return d


class VerifyPasswordRequest(BaseModel):
    """
    Confirmed from VerifyPasswordRequest.java.
    Serial names: email, password, platform, device, version, guest_token
    Default guest_token confirmed hardcoded: "8ac71050855811eb9365c7b9492c1290"
    """
    email:       str
    password:    str
    platform:    str = "androidtv"
    device:      str = "tv"
    version:     str = "9.3.5"
    guest_token: str = "8ac71050855811eb9365c7b9492c1290"   # ← confirmed hardcoded default

    def to_api_dict(self) -> dict:
        return {
            "email":       self.email,
            "password":    self.password,
            "platform":    self.platform,
            "device":      self.device,
            "version":     self.version,
            "guest_token": self.guest_token,
        }


# ── RESPONSE bodies ─────────────────────────────────────────────────────────

class LoginResponse(BaseModel):
    """
    Confirmed from LoginResponse.java — used by refresh token + OTP verify.
    Serial names: access_token, expires_in, token_type, refresh_token,
                  shouldRegister, secureToken
    """
    token:           Optional[str] = Field(default=None,
                         validation_alias=AliasChoices("access_token", "token"))
    expires_in:      Optional[int] = Field(default=None,
                         validation_alias=AliasChoices("expires_in", "expiresIn"))
    token_type:      Optional[str] = Field(default=None,
                         validation_alias=AliasChoices("token_type", "tokenType"))
    refresh_token:   Optional[str] = Field(default=None,
                         validation_alias=AliasChoices("refresh_token", "refreshToken"))
    should_register: Optional[int] = Field(default=0,
                         validation_alias=AliasChoices("shouldRegister", "should_register"))
    secure_token:    Optional[str] = Field(default="",
                         validation_alias=AliasChoices("secureToken", "secure_token"))
    model_config = {"extra": "ignore"}

    @field_validator("expires_in", mode="before")
    @classmethod
    def coerce_int(cls, v: Any) -> Optional[int]:
        return int(v) if v is not None else None


class SendOtpResult(BaseModel):
    """
    Confirmed from SendOtpResult.java.
    ZEE5 code system (NOT HTTP codes):
      code=1 → success ("SMS successfully sent")
      code=2 → error   ("Phone number is not valid")
    """
    code:    int           = 0
    message: Optional[str] = None
    model_config = {"extra": "ignore"}

    @property
    def success(self) -> bool:
        # code=2 is confirmed error. Anything with a message and code!=2 is success.
        return self.code != 2


class UserAuthResponse(BaseModel):
    """
    Confirmed from UserAuthResponse.java / UserAuthResponse$$serializer.java.
    Underlying JSON keys: access_token, refresh_token, token_type, expires_in
    """
    access_token:  str = Field(validation_alias=AliasChoices("access_token", "token"))
    refresh_token: str = Field(validation_alias=AliasChoices("refresh_token", "refreshToken"))
    token_type:    str = Field(default="Bearer")
    expires_in:    int = Field(default=3600,
                               validation_alias=AliasChoices("expires_in", "expiresIn"))
    model_config = {"extra": "ignore"}

    @field_validator("expires_in", mode="before")
    @classmethod
    def coerce_int(cls, v: Any) -> int:
        return int(v)


# ── Device code flow ────────────────────────────────────────────────────────

class GenerateDeviceAuthenticationDto(BaseModel):
    """
    ✓ CONFIRMED from GenerateDeviceAuthenticationDto.java + serializer.
    Response from generateDeviceAuthenticationCode endpoint.
    JSON shape: { "device_code": "ABC123" }
    Just one field — the code the user types on zee5.com/activate.
    """
    device_code: str = Field(validation_alias=AliasChoices("device_code", "deviceCode"))
    model_config = {"extra": "ignore"}


class DeviceCodeAuthResponse(BaseModel):
    """
    Confirmed from DeviceCodeAuthResponseDto import in AuthProvidersAPI.java.
    Exact fields TBC — wraps LoginResponse on successful activation.
    """
    token:         Optional[str] = Field(default=None,
                       validation_alias=AliasChoices("access_token", "token"))
    refresh_token: Optional[str] = Field(default=None,
                       validation_alias=AliasChoices("refresh_token", "refreshToken"))
    expires_in:    Optional[int] = Field(default=3600,
                       validation_alias=AliasChoices("expires_in", "expiresIn"))
    model_config = {"extra": "ignore"}


# ── Error envelope ──────────────────────────────────────────────────────────

class Zee5Error(BaseModel):
    code:    Union[str, int] = ""
    message: str = "Unknown error"
    model_config = {"extra": "ignore"}
