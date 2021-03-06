from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.cache import cache
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Avg
from django.shortcuts import reverse
from django.utils.text import slugify
from django.utils.timezone import localtime, now, timedelta

from string import capwords

from .isbn import meta


class TimeStampedModel(models.Model):
    """Adds created_on, and modified_on Fields to all subclasses"""
    created_on = models.DateTimeField(auto_now_add=True)
    modified_on = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Customer(AbstractUser):
    join_date = models.DateTimeField(auto_now_add=True)
    book_allowance = models.IntegerField(default=3)

    def has_reviewed(self, isbn):
        return self.reviews.filter(book__isbn=isbn).exists()

    def has_book(self, isbn):
        """Returns True if a customer currently has a book"""
        return self.unreturned_loans.filter(
            book_copy__book__isbn=isbn).exists()

    def has_loaned(self, isbn):
        """Returns True if a user has previously loaned a book"""
        return self.loans.filter(returned=True,
                                 book_copy__book__isbn=isbn).exists()

    def get_unreturned_book_loan(self, isbn):
        return self.unreturned_loans.filter(book_copy__book=isbn).first()

    @property
    def overdue_loans(self):
        return self.loans.filter(returned=False, end_date__lte=now())

    @property
    def unreturned_loans(self):
        """Returns Queryset containing a customer unreturned loans"""
        return self.loans.filter(returned=False)

    @property
    def can_loan(self):
        """Returns True if customer is allowed to currently loan books"""
        return self.unreturned_loans.count() < self.book_allowance

    @property
    def read_list(self):
        """Returns set of all books a customer has previously loaned"""
        return Book.objects.filter(copies__loans__customer=self,
                                   copies__loans__returned=True).distinct()

    def get_absolute_url(self):
        return reverse('books:customer-detail')

    def __str__(self):
        return self.username


class Author(models.Model):
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200)

    def save(self, *args, **kwargs):
        self.slug = slugify(self.name)
        super(Author, self).save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('books:author-detail', kwargs={'slug': self.slug})

    def __str__(self):
        return self.name


class Genre(models.Model):
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200)

    class Meta:
        ordering = ('name',)

    def save(self, *args, **kwargs):
        self.slug = slugify(self.name)
        super(Genre, self).save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('books:genre-detail', kwargs={'slug': self.slug})

    def __str__(self):
        return self.name


class BookManager(models.Manager):

    def create_book_from_metadata(self, isbn):
        book, created = self.get_or_create(isbn=isbn)
        if created:

            # Check the cache
            meta_info = cache.get(isbn)
            if not meta_info:
                meta_info = meta(isbn)
                cache.set(isbn, meta_info)

            book.title = capwords(meta_info.get('title', ''))
            book.subtitle = capwords(meta_info.get('subtitle', ''))
            book.img = meta_info.get('img')

            # Book must be saved before associating it with m2m instances
            book.save()

            for name in meta_info.get('authors', []):
                name = capwords(name)
                author, created = Author.objects.get_or_create(name=name)
                book.authors.add(author)

            for name in meta_info.get('categories', []):
                name = capwords(name)
                genre, created = Genre.objects.get_or_create(name=name)
                book.genres.add(genre)

        return book


class Book(TimeStampedModel):
    isbn = models.CharField(max_length=13, primary_key=True)
    title = models.CharField(max_length=200, db_index=True, unique=True)
    subtitle = models.CharField(max_length=200, blank=True)
    img = models.URLField(default='http://placehold.it/150x225')
    slug = models.SlugField(max_length=200)

    objects = BookManager()  # Book specific manager

    authors = models.ManyToManyField('Author', related_name='books')
    genres = models.ManyToManyField('Genre', related_name='books')

    class Meta:
        ordering = ('-created_on',)

    @property
    def average_rating(self):
        return self.reviews.aggregate(Avg('rating'))['rating__avg']

    @property
    def author_names(self):
        return ", ".join(author.name for author in self.authors.all())

    @property
    def num_copies(self):
        return self.copies.count()

    @property
    def num_available_copies(self):
        return self.get_available_copies().count()

    @property
    def is_available(self):
        """Returns True if the Book has any available copies"""
        return self.get_available_copies().exists()

    def get_available_copy(self):
        """Returns first available book copy"""
        return self.get_available_copies().first()

    def get_available_copies(self):
        """Returns queryset of all available book copies"""
        return self.copies.exclude(loans__returned=False)

    def save(self, *args, **kwargs):
        self.slug = slugify(self.title)
        super(Book, self).save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('books:book-detail', kwargs={'slug': self.slug})

    def __str__(self):
        return self.title


class BookCopy(TimeStampedModel):
    book = models.ForeignKey('Book', related_name='copies')

    def __str__(self):
        return '{} Copy'.format(self.book.title)

    @property
    def on_loan(self):
        """Returns True if a book copy has any outstanding loans"""
        return self.loans.filter(returned=False).exists()


class OverdueLoanManager(models.Manager):
    def get_queryset(self):
        return super(OverdueLoanManager, self).get_queryset().filter(
            returned=False, end_date__lte=now()
        )


class Loan(TimeStampedModel):
    start_date = models.DateField()
    end_date = models.DateField()
    returned = models.BooleanField(default=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, blank=True,
                                 null=True, related_name='loans')
    book_copy = models.ForeignKey(
        'BookCopy', on_delete=models.CASCADE, related_name='loans')

    objects = models.Manager()  # The default manager
    overdue = OverdueLoanManager()  # Overdue loan specific manager

    @property
    def is_overdue(self):
        if self.end_date <= localtime(now()).date():
            return True
        return False

    def save(self, *args, **kwargs):
        if not self.start_date and not self.end_date:
            self.start_date = localtime(now()).date()
            self.end_date = self.start_date + timedelta(days=7)
        super(Loan, self).save(*args, **kwargs)

    def __str__(self):
        return str(self.start_date)


class Review(models.Model):
    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)])
    review = models.TextField()
    book = models.ForeignKey('Book', related_name='reviews')
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, blank=True,
                                 null=True, related_name='reviews')

    class Meta:
        # Prevent the same customer writing multiple reviews
        unique_together = ('book', 'customer',)

    def __str__(self):
        return str(self.rating)
