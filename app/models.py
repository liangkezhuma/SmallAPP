import base64
from datetime import datetime, timedelta
from hashlib import md5
import json
import os
from time import time
from flask import current_app, url_for
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import redis
import rq
from app import db, login
from app.search import add_to_index, remove_from_index, query_index


class SearchableMixin(object):
    @classmethod
    def search(cls, expression, page, per_page):
        ids, total = query_index(cls.__tablename__, expression, page, per_page)
        if total == 0:
            return cls.query.filter_by(id=0), 0
        when = []
        for i in range(len(ids)):
            when.append((ids[i], i))
        return cls.query.filter(cls.id.in_(ids)).order_by(
            db.case(when, value=cls.id)), total

    @classmethod
    def before_commit(cls, session):
        session._changes = {
            'add': list(session.new),
            'update': list(session.dirty),
            'delete': list(session.deleted)
        }

    @classmethod
    def after_commit(cls, session):
        for obj in session._changes['add']:
            if isinstance(obj, SearchableMixin):
                add_to_index(obj.__tablename__, obj)
        for obj in session._changes['update']:
            if isinstance(obj, SearchableMixin):
                add_to_index(obj.__tablename__, obj)
        for obj in session._changes['delete']:
            if isinstance(obj, SearchableMixin):
                remove_from_index(obj.__tablename__, obj)
        session._changes = None

    @classmethod
    def reindex(cls):
        for obj in cls.query:
            add_to_index(cls.__tablename__, obj)


db.event.listen(db.session, 'before_commit', SearchableMixin.before_commit)
db.event.listen(db.session, 'after_commit', SearchableMixin.after_commit)


class PaginatedAPIMixin(object):
    @staticmethod
    def to_collection_dict(query, page, per_page, endpoint, **kwargs):
        resources = query.paginate(page, per_page, False)
        data = {
            'items': [item.to_dict() for item in resources.items],
            '_meta': {
                'page': page,
                'per_page': per_page,
                'total_pages': resources.pages,
                'total_items': resources.total
            },
            '_links': {
                'self': url_for(endpoint, page=page, per_page=per_page,
                                **kwargs),
                'next': url_for(endpoint, page=page + 1, per_page=per_page,
                                **kwargs) if resources.has_next else None,
                'prev': url_for(endpoint, page=page - 1, per_page=per_page,
                                **kwargs) if resources.has_prev else None
            }
        }
        return data


followers = db.Table(
    'followers',
    db.Column('follower_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id'))
)


class User(UserMixin, PaginatedAPIMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True)
    email = db.Column(db.String(120), index=True, unique=True)
    password_hash = db.Column(db.String(128))
    posts = db.relationship('Post', backref='author', lazy='dynamic')
    about_me = db.Column(db.String(140))
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    token = db.Column(db.String(32), index=True, unique=True)
    token_expiration = db.Column(db.DateTime)
    followed = db.relationship(
        'User', secondary=followers,
        primaryjoin=(followers.c.follower_id == id),
        secondaryjoin=(followers.c.followed_id == id),
        backref=db.backref('followers', lazy='dynamic'), lazy='dynamic')
    messages_sent = db.relationship('Message',
                                    foreign_keys='Message.sender_id',
                                    backref='author', lazy='dynamic')
    messages_received = db.relationship('Message',
                                        foreign_keys='Message.recipient_id',
                                        backref='recipient', lazy='dynamic')
    last_message_read_time = db.Column(db.DateTime)
    notifications = db.relationship('Notification', backref='user',
                                    lazy='dynamic')
    tasks = db.relationship('Task', backref='user', lazy='dynamic')

    def __repr__(self):
        return '<User {}>'.format(self.username)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def avatar(self, size):
        digest = md5(self.email.lower().encode('utf-8')).hexdigest()
        return 'https://www.gravatar.com/avatar/{}?d=identicon&s={}'.format(
            digest, size)

    def follow(self, user):
        if not self.is_following(user):
            self.followed.append(user)

    def unfollow(self, user):
        if self.is_following(user):
            self.followed.remove(user)

    def is_following(self, user):
        return self.followed.filter(
            followers.c.followed_id == user.id).count() > 0

    def followed_posts(self):
        followed = Post.query.join(
            followers, (followers.c.followed_id == Post.user_id)).filter(
                followers.c.follower_id == self.id)
        own = Post.query.filter_by(user_id=self.id)
        return followed.union(own).order_by(Post.timestamp.desc())

    def get_reset_password_token(self, expires_in=600):
        return jwt.encode(
            {'reset_password': self.id, 'exp': time() + expires_in},
            current_app.config['SECRET_KEY'],
            algorithm='HS256').decode('utf-8')

    @staticmethod
    def verify_reset_password_token(token):
        try:
            id = jwt.decode(token, current_app.config['SECRET_KEY'],
                            algorithms=['HS256'])['reset_password']
        except:
            return
        return User.query.get(id)

    def new_messages(self):
        last_read_time = self.last_message_read_time or datetime(1900, 1, 1)
        return Message.query.filter_by(recipient=self).filter(
            Message.timestamp > last_read_time).count()

    def add_notification(self, name, data):
        self.notifications.filter_by(name=name).delete()
        n = Notification(name=name, payload_json=json.dumps(data), user=self)
        db.session.add(n)
        return n

    def launch_task(self, name, description, *args, **kwargs):
        rq_job = current_app.task_queue.enqueue('app.tasks.' + name, self.id,
                                                *args, **kwargs)
        task = Task(id=rq_job.get_id(), name=name, description=description,
                    user=self)
        db.session.add(task)
        return task

    def get_tasks_in_progress(self):
        return Task.query.filter_by(user=self, complete=False).all()

    def get_task_in_progress(self, name):
        return Task.query.filter_by(name=name, user=self,
                                    complete=False).first()

    def to_dict(self, include_email=False):
        data = {
            'id': self.id,
            'username': self.username,
            'last_seen': self.last_seen.isoformat() + 'Z',
            'about_me': self.about_me,
            'post_count': self.posts.count(),
            'follower_count': self.followers.count(),
            'followed_count': self.followed.count(),
            '_links': {
                'self': url_for('api.get_user', id=self.id),
                'followers': url_for('api.get_followers', id=self.id),
                'followed': url_for('api.get_followed', id=self.id),
                'avatar': self.avatar(128)
            }
        }
        if include_email:
            data['email'] = self.email
        return data

    def from_dict(self, data, new_user=False):
        for field in ['username', 'email', 'about_me']:
            if field in data:
                setattr(self, field, data[field])
        if new_user and 'password' in data:
            self.set_password(data['password'])

    def get_token(self, expires_in=3600):
        now = datetime.utcnow()
        if self.token and self.token_expiration > now + timedelta(seconds=60):
            return self.token
        self.token = base64.b64encode(os.urandom(24)).decode('utf-8')
        self.token_expiration = now + timedelta(seconds=expires_in)
        db.session.add(self)
        return self.token

    def revoke_token(self):
        self.token_expiration = datetime.utcnow() - timedelta(seconds=1)

    @staticmethod
    def check_token(token):
        user = User.query.filter_by(token=token).first()
        if user is None or user.token_expiration < datetime.utcnow():
            return None
        return user


@login.user_loader
def load_user(id):
    return User.query.get(int(id))


class Post(SearchableMixin, db.Model):
    __searchable__ = ['body']
    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.String(140))
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    language = db.Column(db.String(5))

    def __repr__(self):
        return '<Post {}>'.format(self.body)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    body = db.Column(db.String(140))
    timestamp = db.Column(db.DateTime, index=True, default=datetime.utcnow)

    def __repr__(self):
        return '<Message {}>'.format(self.body)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    timestamp = db.Column(db.Float, index=True, default=time)
    payload_json = db.Column(db.Text)

    def get_data(self):
        return json.loads(str(self.payload_json))


class Task(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(128), index=True)
    description = db.Column(db.String(128))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    complete = db.Column(db.Boolean, default=False)

    def get_rq_job(self):
        try:
            rq_job = rq.job.Job.fetch(self.id, connection=current_app.redis)
        except (redis.exceptions.RedisError, rq.exceptions.NoSuchJobError):
            return None
        return rq_job

    def get_progress(self):
        job = self.get_rq_job()
        return job.meta.get('progress', 0) if job is not None else 100


class Categories(UserMixin, PaginatedAPIMixin, db.Model):
    __tablename__ = 'categories'
    category_id = db.Column(db.Integer, primary_key=True)
    category_name = db.Column(db.String(255), index=True)

    def from_dict(self, data, new_category=False):
        for field in ['category_name']:
            if field in data:
                setattr(self, field, data[field])

    def to_dict(self):
        data = {
            'category_id': self.category_id,
            'category_name': self.category_name
        }
        return data

    def __repr__(self):
        return '<Category {}>'.format(self.category_name)


class Brands(UserMixin, PaginatedAPIMixin, db.Model):
    __tablename__ = 'brands'
    brand_id = db.Column(db.Integer, primary_key=True)
    brand_name = db.Column(db.String(255), index=True)

    def from_dict(self, data, new_brand=False):
        for field in ['brand_name']:
            if field in data:
                setattr(self, field, data[field])

    def to_dict(self):
        data = {
            'brand_id': self.brand_id,
            'brand_name': self.brand_name
        }
        return data

    def __repr__(self):
        return '<Brand {}>'.format(self.brand_name)


class Products(UserMixin, PaginatedAPIMixin, db.Model):
    __tablename__ = 'products'
    product_id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(255))
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.brand_id'))
    category_id = db.Column(
        db.Integer, db.ForeignKey('categories.category_id'))
    model_year = db.Column(db.Integer)
    list_price = db.Column(db.Float)

    def from_dict(self, data, new_product=False):
        for field in [
                'product_id', 'product_name',
                'brand_id', 'category_id', 'model_year', 'list_price'
                ]:
            if field in data:
                setattr(self, field, data[field])

    def to_dict(self):
        data = {
            'product_id': self.product_id,
            'product_name': self.product_name,
            'brand_id': self.brand_id,
            'category_id': self.category_id,
            'model_year': self.model_year,
            'list_price': self.list_price
        }
        return data

    def __repr__(self):
        return '<Product {}>'.format(self.product_name)


class Stocks(UserMixin, PaginatedAPIMixin, db.Model):
    __tablename__ = 'stocks'
    store_id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.product_id'))
    quantity = db.Column(db.Integer)


class Stores(UserMixin, PaginatedAPIMixin, db.Model):
    __tablename__ = 'stores'
    store_id = db.Column(db.Integer, primary_key=True)
    store_name = db.Column(db.String(255))
    phone = db.Column(db.String(25))
    email = db.Column(db.String(255))
    street = db.Column(db.String(255))
    city = db.Column(db.String(255))
    state = db.Column(db.String(10))
    zip_code = db.Column(db.String(10))

    def from_dict(self, data, new_store=False):
        for field in [
                'store_name', 'phone', 'email', 'street', 'city',
                'state', 'zip_code']:
            if field in data:
                setattr(self, field, data[field])

    def to_dict(self):
        data = {
            'store_id': self.store_id,
            'store_name': self.store_name,
            'phone': self.phone,
            'email': self.email,
            'street': self.street,
            'city': self.city,
            'state': self.state,
            'zip_code': self.zip_code
        }
        return data

    def __repr__(self):
        return '<Store {}>'.format(self.store_name)


class Orders(UserMixin, PaginatedAPIMixin, db.Model):
    __tablename__ = 'orders'
    order_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    # Order status: 1 = Pending; 2 = Processing; 3 = Rejected; 4 = Completed
    order_status = db.Column(db.Integer)
    order_date = db.Column(db.DateTime, default=datetime.utcnow)
    required_date = db.Column(db.DateTime, default=datetime.utcnow)
    shipped_date = db.Column(db.DateTime, default=datetime.utcnow)
    store_id = db.Column(db.Integer, db.ForeignKey('stores.store_id'))
    staff_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    order_items = db.relationship(
        'Order_items', backref='product', lazy='dynamic')

    def from_dict(self, data, new_order=False):
        for field in [
                'customer_id', 'order_status', 'order_date',
                'required_date', 'shipped_date', 'store_id', 'staff_id'
                ]:
            if field in data:
                setattr(self, field, data[field])

    def to_dict(self):
        data = {
            'order_id': self.order_id,
            'customer_id': self.customer_id,
            'order_status': self.order_status,
            'order_date': self.order_date,
            'required_date': self.required_date,
            'shipped_date': self.shipped_date,
            'store_id': self.store_id,
            'staff_id': self.staff_id,
            'order_items': [item.to_dict() for item in self.order_items]
        }
        return data

    def __repr__(self):
        return '<Order {}>'.format(self.order_id)


class Order_items(UserMixin, PaginatedAPIMixin, db.Model):
    __tablename__ = 'order_items'
    order_id = db.Column(
        db.Integer, db.ForeignKey('orders.order_id'), primary_key=True)
    item_id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.product_id'))
    quantity = db.Column(db.Integer)
    list_price = db.Column(db.Integer)
    discount = db.Column(db.Integer)
    Orders = db.relationship("Orders", back_populates="order_items")

    def from_dict(self, data, new_item=False):
        for field in [
                'product_id', 'quantity', 'list_price',
                'discount', 'order']:
            if field in data:
                setattr(self, field, data[field])

    def to_dict(self):
        data = {
            'order_id': self.order_id,
            'item_id': self.item_id,
            'product_id': self.product_id,
            'quantity': self.quantity,
            'list_price': self.list_price,
            'discount': self.discount
        }
        return data

    def __repr__(self):
        return '<Brand {}>'.format(self.brand_name)


# Orders.order_items = db.relationship(
#     "Order_items", order_by=Order_items.item_id, back_populates="order_items")
