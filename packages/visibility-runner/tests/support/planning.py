"""Synthetic planning builders shared by planning and scheduler tests."""
from __future__ import annotations
from visibility_runner import Area, Clinic, ClinicArchetype, Geography, MeasurementConfig, Platform, ProfileIdentity, RetryPolicy, Treatment
from visibility_runner.planning import build_run_plan


MANIFEST_HASH = "a" * 64


def clinic() -> Clinic:
    return Clinic(
        name="Example Clinic",
        account_id="clinic-001",
        archetype=ClinicArchetype(
            id="aesthetics-skin-clinic",
            singular="aesthetics and skin clinic",
            plural="aesthetics and skin clinics",
        ),
    )


def area(
    name: str = "Marylebone",
    *,
    area_id: str = "marylebone",
    branch_id: str = "branch-marylebone",
    treatment_names: tuple[str, ...] = ("fat freezing",),
    greater_area: str = "Central London",
    station: str | None = None,
    postcode: str | None = None,
) -> Area:
    return Area(
        name=name,
        id=area_id,
        branch_id=branch_id,
        branch_name=f"Example Clinic {name}",
        greater_area=Geography(
            name=greater_area,
            id=greater_area.casefold().replace(" ", "-"),
        ),
        city=Geography(name="London", id="london"),
        treatments=tuple(
            Treatment(name=treatment_name)
            for treatment_name in treatment_names
        ),
        station=station or f"{name} station",
        postcode=postcode or ("W1H" if area_id == "marylebone" else "N1"),
    )


def config(
    *,
    areas: tuple[Area, ...] | None = None,
    repetitions: int = 1,
    platforms: tuple[Platform, ...] = (
        Platform.CHATGPT,
        Platform.GEMINI,
        Platform.GOOGLE_AI_OVERVIEW,
    ),
) -> MeasurementConfig:
    return MeasurementConfig(
        clinic=clinic(),
        areas=areas or (area(),),
        repetitions=repetitions,
        platforms=platforms,
    )


def profiles() -> tuple[ProfileIdentity, ...]:
    return (
        ProfileIdentity(
            id="profile_one",
            display_name="Profile One",
            chrome_directory="Profile 1",
        ),
        ProfileIdentity(
            id="profile_two",
            display_name="Profile Two",
            chrome_directory="Profile 2",
        ),
    )


def plan(
    value: MeasurementConfig,
    *,
    run_id: str = "planning-contract",
    retry_policy: RetryPolicy | None = None,
):
    identities = profiles()
    return build_run_plan(
        value,
        run_id=run_id,
        selected_profile_ids=tuple(row.id for row in identities),
        selected_profiles=identities,
        profile_manifest_sha256=MANIFEST_HASH,
        retry_policy=retry_policy,
    )
