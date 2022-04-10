"""rename url fields

Revision ID: 09b041966a84
Revises: da63661dc19a
Create Date: 2022-04-09 20:29:11.328013

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
from sqlalchemy import table, column, String

revision = '09b041966a84'
down_revision = 'da63661dc19a'
branch_labels = None
depends_on = None


episodes = table("podcast_episodes", column("remote_url", String), column("file_path", String), )


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_podcast_episodes_created_by_id', table_name='podcast_episodes')
    op.create_index(op.f('ix_podcast_episodes_owner_id'), 'podcast_episodes', ['owner_id'], unique=False)
    op.alter_column('podcast_episodes', "file_name", new_column_name="file_path")
    op.execute(episodes.update().values({"file_path": episodes.remote_url}))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_podcast_episodes_owner_id'), table_name='podcast_episodes')
    op.create_index('ix_podcast_episodes_created_by_id', 'podcast_episodes', ['owner_id'], unique=False)
    op.alter_column('podcast_episodes', "file_path", new_column_name="file_name")
    # ### end Alembic commands ###
