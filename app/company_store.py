import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from app.models import CompanyProfile

logger = logging.getLogger(__name__)


class CompanyStore:
    """Loads and manages YAML company profiles from the companies/ directory."""

    def __init__(self, companies_dir: Path) -> None:
        self.companies_dir = companies_dir
        self._profiles: Dict[str, CompanyProfile] = {}
        self.reload()

    def reload(self) -> int:
        profiles: Dict[str, CompanyProfile] = {}
        if not self.companies_dir.exists():
            logger.warning("Companies directory does not exist: %s", self.companies_dir)
            self._profiles = {}
            return 0

        for path in sorted(self.companies_dir.glob("*.yaml")):
            if path.name.startswith("_"):
                continue
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                profile = CompanyProfile.model_validate(raw)
                profiles[profile.slug] = profile
            except Exception as exc:
                logger.error("Failed to load company profile %s: %s", path.name, exc)

        self._profiles = profiles
        logger.info("Loaded %d company profile(s)", len(self._profiles))
        return len(self._profiles)

    def get(self, slug: str) -> Optional[CompanyProfile]:
        return self._profiles.get(slug)

    def get_or_default(self, slug: Optional[str], default_slug: str) -> Optional[CompanyProfile]:
        if slug and slug in self._profiles:
            return self._profiles[slug]
        if default_slug in self._profiles:
            return self._profiles[default_slug]
        return next(iter(self._profiles.values()), None)

    def get_or_default_for_tenant(
        self,
        tenant_slug: Optional[str],
        requested_slug: Optional[str],
        fallback_slug: Optional[str] = None,
    ) -> Optional[CompanyProfile]:
        """Профиль компании в пределах одного клиента.

        Порядок: запрошенный slug (если принадлежит клиенту) → fallback_slug →
        первая компания клиента.
        """
        allowed = self.list_for_tenant(tenant_slug)
        if not allowed:
            return None
        if requested_slug:
            for profile in allowed:
                if profile.slug == requested_slug:
                    return profile
        if fallback_slug:
            for profile in allowed:
                if profile.slug == fallback_slug:
                    return profile
        return allowed[0]

    def list_all(self) -> List[CompanyProfile]:
        return list(self._profiles.values())

    def list_for_tenant(self, tenant_slug: Optional[str]) -> List[CompanyProfile]:
        """Компании конкретного клиента. None — все (платформенный администратор)."""
        if tenant_slug is None:
            return list(self._profiles.values())
        return [p for p in self._profiles.values() if (p.tenant_slug or "default") == tenant_slug]

    def slugs_for_tenant(self, tenant_slug: Optional[str]) -> Optional[List[str]]:
        """Список слагов клиента для фильтрации данных. None — без ограничений."""
        if tenant_slug is None:
            return None
        return [p.slug for p in self.list_for_tenant(tenant_slug)]

    def owns_company(self, tenant_slug: Optional[str], slug: str) -> bool:
        if tenant_slug is None:
            return True
        profile = self._profiles.get(slug)
        return bool(profile) and (profile.tenant_slug or "default") == tenant_slug

    def save(self, profile: CompanyProfile) -> Path:
        self.companies_dir.mkdir(parents=True, exist_ok=True)
        target = self.companies_dir / f"{profile.slug}.yaml"
        data = profile.model_dump()
        target.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self._profiles[profile.slug] = profile
        return target

    def delete(self, slug: str) -> bool:
        target = self.companies_dir / f"{slug}.yaml"
        deleted = False
        if target.exists():
            target.unlink()
            deleted = True
        if slug in self._profiles:
            del self._profiles[slug]
            deleted = True
        return deleted
