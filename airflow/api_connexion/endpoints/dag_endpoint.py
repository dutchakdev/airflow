# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from typing import Collection, Optional

from flask import current_app, g, request
from marshmallow import ValidationError
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import or_

from airflow import DAG
from airflow._vendor.connexion import NoContent
from airflow.api_connexion import security
from airflow.api_connexion.exceptions import AlreadyExists, BadRequest, NotFound
from airflow.api_connexion.parameters import check_limit, format_parameters
from airflow.api_connexion.schemas.dag_schema import (
    DAGCollection,
    dag_detail_schema,
    dag_schema,
    dags_collection_schema,
)
from airflow.api_connexion.types import APIResponse, UpdateMask
from airflow.exceptions import AirflowException, DagNotFound
from airflow.models.dag import DagModel, DagTag
from airflow.security import permissions
from airflow.utils.session import NEW_SESSION, provide_session


@security.requires_access([(permissions.ACTION_CAN_READ, permissions.RESOURCE_DAG)])
@provide_session
def get_dag(*, dag_id: str, session: Session = NEW_SESSION) -> APIResponse:
    """Get basic information about a DAG."""
    dag = session.query(DagModel).filter(DagModel.dag_id == dag_id).one_or_none()

    if dag is None:
        raise NotFound("DAG not found", detail=f"The DAG with dag_id: {dag_id} was not found")

    return dag_schema.dump(dag)


@security.requires_access([(permissions.ACTION_CAN_READ, permissions.RESOURCE_DAG)])
def get_dag_details(*, dag_id: str) -> APIResponse:
    """Get details of DAG."""
    dag: DAG = current_app.dag_bag.get_dag(dag_id)
    if not dag:
        raise NotFound("DAG not found", detail=f"The DAG with dag_id: {dag_id} was not found")
    return dag_detail_schema.dump(dag)


@security.requires_access([(permissions.ACTION_CAN_READ, permissions.RESOURCE_DAG)])
@format_parameters({'limit': check_limit})
@provide_session
def get_dags(
    *,
    limit: int,
    offset: int = 0,
    tags: Optional[Collection[str]] = None,
    dag_id_pattern: Optional[str] = None,
    only_active: bool = True,
    session: Session = NEW_SESSION,
) -> APIResponse:
    """Get all DAGs."""
    if only_active:
        dags_query = session.query(DagModel).filter(~DagModel.is_subdag, DagModel.is_active)
    else:
        dags_query = session.query(DagModel).filter(~DagModel.is_subdag)

    if dag_id_pattern:
        dags_query = dags_query.filter(DagModel.dag_id.ilike(f'%{dag_id_pattern}%'))

    readable_dags = current_app.appbuilder.sm.get_accessible_dag_ids(g.user)

    dags_query = dags_query.filter(DagModel.dag_id.in_(readable_dags))
    if tags:
        cond = [DagModel.tags.any(DagTag.name == tag) for tag in tags]
        dags_query = dags_query.filter(or_(*cond))

    total_entries = len(dags_query.all())

    dags = dags_query.order_by(DagModel.dag_id).offset(offset).limit(limit).all()

    return dags_collection_schema.dump(DAGCollection(dags=dags, total_entries=total_entries))


@security.requires_access([(permissions.ACTION_CAN_EDIT, permissions.RESOURCE_DAG)])
@provide_session
def patch_dag(*, dag_id: str, update_mask: UpdateMask = None, session: Session = NEW_SESSION) -> APIResponse:
    """Update the specific DAG"""
    dag = session.query(DagModel).filter(DagModel.dag_id == dag_id).one_or_none()
    if not dag:
        raise NotFound(f"Dag with id: '{dag_id}' not found")
    try:
        patch_body = dag_schema.load(request.json, session=session)
    except ValidationError as err:
        raise BadRequest("Invalid Dag schema", detail=str(err.messages))
    if update_mask:
        patch_body_ = {}
        if len(update_mask) > 1:
            raise BadRequest(detail="Only `is_paused` field can be updated through the REST API")
        update_mask = update_mask[0]
        if update_mask != 'is_paused':
            raise BadRequest(detail="Only `is_paused` field can be updated through the REST API")
        patch_body_[update_mask] = patch_body[update_mask]
        patch_body = patch_body_
    setattr(dag, 'is_paused', patch_body['is_paused'])
    session.commit()
    return dag_schema.dump(dag)


@security.requires_access([(permissions.ACTION_CAN_DELETE, permissions.RESOURCE_DAG)])
@provide_session
def delete_dag(dag_id: str, session: Session = NEW_SESSION) -> APIResponse:
    """Delete the specific DAG."""
    # TODO: This function is shared with the /delete endpoint used by the web
    # UI, so we're reusing it to simplify maintenance. Refactor the function to
    # another place when the experimental/legacy API is removed.
    from airflow.api.common.experimental import delete_dag as delete_dag_module

    try:
        delete_dag_module.delete_dag(dag_id, session=session)
    except DagNotFound:
        raise NotFound(f"Dag with id: '{dag_id}' not found")
    except AirflowException:
        raise AlreadyExists(detail=f"Task instances of dag with id: '{dag_id}' are still running")

    return NoContent, 204
